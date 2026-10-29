#!/usr/bin/env python3
"""Disposable Docker Slurm-only acceptance for execution-helper upgrades.

Submits two bounded stdlib jobs under an installed reviewed bundle with a
comment-only version difference, then uses the current management bundle. One
response is deliberately lost. This checks the native adapter/journal boundary;
the normal HTTP smoke separately checks admission and transactional settlement.
Never use institutional credentials or a production scheduler with this tool.
"""

import asyncio

import argparse
import hashlib
import json
from pathlib import Path
import time
from unittest.mock import patch
from uuid import uuid4

from compute_relay.backends import BackendError, SlurmBackend, prepare, submission_helper_is_current
from compute_relay.remote import bundles
from compute_relay.transports import OpenSSHTransport, TransportError
from compute_relay.worker.enrollment import resolve_connection


class FixtureTransport:
    def __init__(self, connection):
        self.transport = OpenSSHTransport(connection)
        self.submissions = 0
        self.lose_next_response = False

    async def call_helper(self, source, request):
        result = await self.transport.call_helper(source, request)
        if request["operation"] == "submit":
            self.submissions += 1
            if self.lose_next_response:
                self.lose_next_response = False
                raise TransportError("Synthetic lost fixture reply")
        return result


def fixture_connection(path):
    config = json.loads(path.read_bytes())
    # The normal fixture advertises a protected profile and enrolls connections
    # dynamically. Resolve only this fixed disposable target through that same
    # reviewed profile validator, without reading or printing key contents.
    connection, _, _ = resolve_connection({"backend": "slurm", "credential_profile": "demo-slurm",
        "ssh": {"host": "slurm-fixture", "user": "brokerjob", "port": 22},
        "remote_root": "/home/brokerjob/jobs"}, config["credential_profiles"])
    ssh = connection.get("ssh", {})
    if (ssh.get("host") != "slurm-fixture" or ssh.get("user") != "brokerjob"
            or not ssh.get("identity_file", "").startswith("/run/fixture-ssh/")
            or not ssh.get("known_hosts", "").startswith("/run/fixture-ssh/")
            or not connection.get("remote_root", "").startswith("/home/brokerjob/")):
        raise RuntimeError("Target is not the dedicated disposable fixture")
    return connection


def make_package(connection):
    source = b"import time\nprint('HELPER_UPGRADE_FIXTURE', flush=True)\ntime.sleep(40)\n"
    environment = {"id": str(uuid4()), "revision_sha256": "a" * 64, "interpreter": "/usr/bin/python3"}
    request = {"schema_version": "1.0", "project_id": str(uuid4()), "connection_id": str(uuid4()),
               "name": "helper-upgrade-fixture", "environment": {key: environment[key] for key in ("id", "revision_sha256")},
               "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
               "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
               "slurm": {"partition": "debug", "account": "broker"}, "outputs": [],
               "files": [{"path": "main.py", "sha256": hashlib.sha256(source).hexdigest(), "size_bytes": len(source), "executable": False}]}
    package = prepare(request, environment, connection, str(uuid4()))
    return package["manifest"], {"main.py": source, **{path: text.encode() for path, text in package["generated_files"].items()}}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-only", action="store_true", required=True)
    parser.add_argument("--worker-config", type=Path, default=Path("/run/demo/worker.json"))
    args = parser.parse_args()
    connection = fixture_connection(args.worker_config)
    transport = FixtureTransport(connection)
    backend = SlurmBackend(connection, transport=transport)
    original = bundles._component
    attempts = []
    try:
        for lose_reply in (False, True):
            attempt_id = str(uuid4())
            with patch.object(bundles, "_component", side_effect=lambda name: original(name) + ("# approved fixture version N\n" if name == "jobs.py" else "")):
                manifest, files = make_package(connection)
                transport.lose_next_response = lose_reply
                submitted = await backend.submit(attempt_id, manifest, files)
            if submitted["outcome"] != ("submission_uncertain" if lose_reply else "submitted"):
                raise RuntimeError("Fixture did not reach expected submission state")
            if submission_helper_is_current(manifest):
                raise RuntimeError("Fixture did not change execution-helper version")
            recovered = await backend.reconcile(attempt_id, manifest)
            if recovered.get("outcome") != "submitted":
                raise RuntimeError("Current management could not recover old attempt")
            native = recovered["native_identity"]
            attempts.append((native, manifest))
            try:
                await backend.submit(attempt_id, manifest, files)
            except BackendError:
                pass
            else:
                raise RuntimeError("Old package unexpectedly permitted a new submission call")
            if (await backend.cancel(native, manifest=manifest)).get("outcome") != "cancel_requested":
                raise RuntimeError("Current management did not acknowledge native cancellation")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                observation = (await backend.observe([{"native_identity": native, "manifest": manifest}]))[native["attempt_id"]]
                if observation.get("terminal") is True and observation.get("accounting", {}).get("final") is True:
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Current management did not obtain final native accounting")
            await backend.output_manifest(native, manifest=manifest)
            if (await backend.reconcile(attempt_id, manifest)).get("native_identity") != native:
                raise RuntimeError("Repeated recovery changed native identity")
        if transport.submissions != 2:
            raise RuntimeError("Recovery submitted an additional native job")
        print(json.dumps({"helper_upgrade": "passed", "native_submissions": 2,
                          "lost_reply_recovered": True, "management_operations": ["reconcile", "cancel", "observe", "output_manifest"],
                          "final_native_accounting": True}, sort_keys=True))
    finally:
        for native, manifest in attempts:
            await backend.cancel(native, manifest=manifest)


if __name__ == "__main__":
    asyncio.run(main())
