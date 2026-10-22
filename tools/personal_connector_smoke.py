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
        command = [sys.executable, "-m", "compute_broker.cli", "connector", "--state-directory", str(root),
            "--non-interactive", "--allow-insecure-http"]
        log = root / "connector.log"
        with log.open("wb") as output:
            run.stage = "connector_start_and_discovery"
            worker = subprocess.Popen([*command, "--setup", str(path)], stdin=subprocess.DEVNULL, stdout=output, stderr=output)
            try:
                deadline = time.monotonic() + 45
                agent = Client(base, token=(args.demo_file.parent / "agent-token").read_text().strip())
                while time.monotonic() < deadline:
                    require(worker.poll() is None, "Personal connector exited during pairing or discovery")
                    entries = agent.request("GET", "/api/v1/connections")["connections"]
                    if (root / "state.json").exists() and any(row["id"] == demo["connection_id"] and row["auth_state"] == "ready" for row in entries):
                        break
                    time.sleep(0.25)
                else:
                    raise SmokeFailure("Personal connector identification timed out")
                state = json.loads((root / "state.json").read_bytes())
                require(state["connection_id"] == demo["connection_id"], "Pairing replaced the cluster identity")
                # Real submitted Python, result hashes and final Slurm accounting
                # all travel through the newly established master socket.
                run.stage = "reviewed_native_job"
                job_smoke(base, args.demo_file, 90)
            finally:
                worker.terminate()
                worker.wait(timeout=15)
        run.stage = "shutdown_and_resume"
        require(not (root / "s").exists(), "Connector shutdown left its authenticated master socket")
        original = json.loads((root / "state.json").read_bytes())
        resumed = subprocess.run([*command, "--once"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        require(resumed.returncode == 0, "Personal connector could not resume its saved worker identity")
        require(json.loads((root / "state.json").read_bytes())["worker_id"] == original["worker_id"], "Resume registered another worker")
        require(not (root / "s").exists(), "Resume shutdown left an authenticated socket")
        print(json.dumps({"ok": True, "fixture_only": True, "automatic_worker_pairing": True,
            "native_ssh_master": True, "discovery": True, "reviewed_slurm_execution": True,
            "same_worker_on_resume": True, "master_closed_on_shutdown": True}))


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(json.dumps({"ok": False, "error_type": type(error).__name__, "stage": getattr(run, "stage", "start"),
            "message": str(error) if isinstance(error, SmokeFailure) else "Fixture check failed"}), file=sys.stderr)
        raise SystemExit(1) from None
