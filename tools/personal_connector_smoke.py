#!/usr/bin/env python3
"""Synthetic Docker-only personal connector acceptance; no institutional keys."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from container_smoke import Client, SmokeFailure, require, run as job_smoke


def run():
    run.stage = "read_fixture_metadata"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-file", type=Path, default=Path("/run/demo/demo.json"))
    args = parser.parse_args()
    demo = json.loads(args.demo_file.read_bytes())
    base = "http://control:8765"
    run.stage = "human_login"
    human = Client(base);human.login(demo["owner_username"], demo["owner_password"])
    # Adopt the existing synthetic connection so its project and curated recipe
    # remain usable, while replacing its manually bootstrapped worker binding.
    run.stage = "download_setup"
    page = human.request("GET", f"/clusters/{demo['connection_id']}/", binary=True)
    import re
    revision = re.search(rb'name="setup_revision" value="([0-9]+)"', page)
    setup = human.request("POST", f"/clusters/{demo['connection_id']}/connect/",
        {"setup_revision": revision.group(1).decode()} if revision else {}, form=True)
    require("pairing_token" in setup and "no-store" in human.last_headers.get("cache-control", ""), "Setup did not return a private pairing document")
    setup["local"] = {"identity_file": "/run/fixture-ssh/id_ed25519", "known_hosts": "/run/fixture-ssh/known_hosts", "session_seconds": 300}
    with tempfile.TemporaryDirectory(prefix="cb-", dir="/tmp") as temp:
        root = Path(temp);root.chmod(0o700)
        path = root / "setup.json";path.write_text(json.dumps(setup));path.chmod(0o600)
        directory = root / "connectors" / "lab"
        command = [sys.executable, "-m", "compute_broker.cli", "connector", "--state-directory", str(directory),
            "--non-interactive", "--allow-insecure-http"]
        log = root / "connector.log"
        with log.open("wb") as output:
            run.stage = "connector_start_and_discovery"
            worker = subprocess.Popen([*command, "--setup", str(path)], stdin=subprocess.DEVNULL, stdout=output, stderr=output, umask=0o002)
            try:
                deadline = time.monotonic() + 45
                agent = Client(base, token=(args.demo_file.parent / "agent-token").read_text().strip())
                while time.monotonic() < deadline:
                    require(worker.poll() is None, "Personal connector exited during pairing or discovery")
                    entries = agent.request("GET", "/api/v1/connections")["connections"]
                    if (directory / "state.json").exists() and any(row["id"] == demo["connection_id"] and row["auth_state"] == "ready" for row in entries):
                        break
                    time.sleep(0.25)
                else:
                    raise SmokeFailure("Personal connector identification timed out")
                require(all(p.stat().st_mode & 0o777 == 0o700 for p in [root / "connectors", directory]), "Nested state was not created privately")
                state = json.loads((directory / "state.json").read_bytes())
                require(state["connection_id"] == demo["connection_id"], "Pairing replaced the cluster identity")
                # Real submitted Python, result hashes and final Slurm accounting
                # all travel through the newly established master socket.
                run.stage = "reviewed_native_job"
                job_smoke(base, args.demo_file, 90)
            finally:
                worker.terminate()
                worker.wait(timeout=15)
        run.stage = "shutdown_and_resume"
        require(not (directory / "s").exists(), "Connector shutdown left its authenticated master socket")
        original = json.loads((directory / "state.json").read_bytes())
        resumed = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, umask=0o002)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not (directory / "s").exists():
                require(resumed.poll() is None, "Personal connector could not resume its saved worker identity")
                time.sleep(0.1)
            require((directory / "s").exists(), "Resumed SSH master was not established")
            require(json.loads((directory / "state.json").read_bytes())["worker_id"] == original["worker_id"], "Resume registered another worker")
            run.stage = "remove_configuration_and_revoke"
            removal_path = f"/clusters/{demo['connection_id']}/remove/"
            confirmation = human.request("GET", removal_path, binary=True)
            fields = {name.decode(): value.decode() for name, value in re.findall(
                rb'name="(setup_revision|connection_revision_sha256)" value="([^"]*)"', confirmation)}
            human.request("POST", removal_path, fields, form=True, binary=True)
            resumed.wait(timeout=15)
            require(resumed.returncode == 0 and not (directory / "s").exists(), "Revocation left the connector or its SSH master active")
            require(not agent.request("GET", "/api/v1/connections")["connections"], "Removed configuration remains executable")
        finally:
            if resumed.poll() is None:
                resumed.terminate();resumed.wait(timeout=15)
        print(json.dumps({"ok": True, "fixture_only": True, "automatic_worker_pairing": True,
            "native_ssh_master": True, "nested_private_state": True, "discovery": True, "reviewed_slurm_execution": True,
            "same_worker_on_resume": True, "master_closed_on_shutdown": True,
            "removed_configuration": True, "master_closed_on_revocation": True}))


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(json.dumps({"ok": False, "error_type": type(error).__name__, "stage": getattr(run, "stage", "start"),
            "message": str(error) if isinstance(error, SmokeFailure) else "Fixture check failed"}), file=sys.stderr)
        raise SystemExit(1) from None
