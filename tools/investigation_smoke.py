#!/usr/bin/env python3
"""One reviewed Slurm fixture job, grouped closeout and the installed files CLI."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from uuid import uuid4

from container_smoke import Client, SmokeFailure, require, revealed_key


def run(base_url, demo_file, timeout):
    demo = json.loads(demo_file.read_bytes())
    target = demo.get("cluster_form", {})
    require(demo.get("backend") == "slurm" and target.get("host") == "slurm-fixture"
        and target.get("user") == "brokerjob" and target.get("remote_root") == "/home/brokerjob/jobs",
        "Investigation acceptance requires explicit disposable Slurm fixture metadata")
    human = Client(base_url)
    human.login(demo["owner_username"], demo["owner_password"])
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    human.request("POST", "/projects/new/", {"name": "Disposable investigation " + uuid4().hex[:12],
        "mode": "approval_required", "cpu_hours": "2", "gpu_hours": "0", "max_cpus": "1", "max_gpus": "0",
        "memory_mib": "128", "wall_minutes": "1", "charge_minutes": "2", "max_active_jobs": "2", "max_total_jobs": "2",
        "expires_at": expiry, "connections": [demo["connection_id"]], "environments": [demo["environment_id"]],
        "allowed_gpu_types": []}, form=True, binary=True)
    match = re.fullmatch(r"/projects/([0-9a-f-]{36})/", urllib.parse.urlsplit(human.last_url).path)
    require(match is not None, "Investigation project creation failed")
    project_id = match.group(1)
    page = human.request("POST", f"/projects/{project_id}/keys/new/", {"name": "Disposable investigation agent",
        "expires_at": expiry, "scopes": ["investigations:write", "objects:write", "jobs:write", "jobs:read", "jobs:execute", "jobs:cancel", "artifacts:read"],
        "cluster_access": "inherit", "allowed_connections": [], "execution_mode": "approval_required",
        "submission_id": str(uuid4())}, form=True, binary=True)
    secret, _ = revealed_key(human, page)
    agent = Client(base_url, token=secret)
    require("investigations" in agent.request("GET", "/api/v1/capabilities")["enabled_extensions"], "Investigation module unavailable")
    group = agent.request("POST", f"/api/v1/projects/{project_id}/investigations", {"name": "bounded-science",
        "plan": {"question": "Can the fixture preserve a result and reconcile its run?", "deliverable": "Verified answer and exported provenance",
            "core_experiments": ["one small CPU stage"], "required_validation": ["answer is 42; hashes and accounting reconcile"],
            "stopping_rule": "Stop after the single core stage and validation; cancel unused packages.",
            "budget": {"cpu_core_seconds": 240, "gpu_device_seconds": 0, "max_executions": 2}, "diagnostic_limit": 1}})
    source = b"from pathlib import Path\nPath('answer.json').write_bytes(b'{\"answer\":42}\\n')\n"
    descriptor = agent.request("POST", f"/api/v1/projects/{project_id}/objects", {"path": "main.py", "content_base64": base64.b64encode(source).decode()})
    jobs = []
    for name in ("core", "abandoned", "pending-review"):
        request = {"schema_version": "1.0", "project_id": project_id, "connection_id": demo["connection_id"], "name": name,
            "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
            "environment": {"id": demo["environment_id"], "revision_sha256": demo["environment_revision_sha256"]},
            "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
            "slurm": {"partition": "debug", "account": "broker"}, "files": [descriptor], "outputs": ["answer.json"]}
        job = agent.request("POST", "/api/v1/jobs", {"request": request,
            "context": {"investigations": {"id": group["id"], "role": "core" if name == "core" else "exploration"}}})
        agent.request("POST", f"/api/v1/jobs/{job['id']}/prepare", {})
        jobs.append(job)
    for job in (jobs[0], jobs[2]):
        agent.request("POST", f"/api/v1/jobs/{job['id']}/execution-requests", {})
    core = agent.request("GET", f"/api/v1/jobs/{jobs[0]['id']}")
    require(core["execution_progress"]["compute_submitted"] is False, "Pending approval reported as submitted")
    decision = human.request("POST", f"/api/v1/jobs/{core['id']}/decisions", {"execution_request_id": core["execution_requests"][0]["id"],
        "manifest_sha256": core["manifest_sha256"], "approved": True, "comment": "Approve the single disposable investigation stage."})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = agent.request("GET", f"/api/v1/jobs/{core['id']}")
        require(len(current["attempts"]) == 1 and current["attempts"][0]["state"] not in {"failed", "cancelled", "expired"}, "Fixture execution failed or duplicated")
        if current["completion"]["operationally_reconciled"]:
            break
        time.sleep(1)
    else:
        raise SmokeFailure("Investigation stage failed to reconcile before the deadline")
    require(current["attempts"][0]["id"] == decision["attempt_id"] and current["execution_progress"]["compute_submitted"] is True,
        "Completed stage lost its native submission identity")
    before = agent.request("GET", f"/api/v1/investigations/{group['id']}")
    waited = agent.request("GET", f"/api/v1/investigations/{group['id']}?after_revision={before['revision']}&wait_seconds=1")
    require(not waited["changed"], "Unchanged grouped status did not honor revision waiting")
    agent.request("POST", f"/api/v1/investigations/{group['id']}/finish", {"conclusion": "sufficient",
        "report": "One stage completed; verify the answer and manifest locally. No further experiments needed.", "cancel_unstarted": True})
    with tempfile.TemporaryDirectory(prefix="relay-investigation-") as temporary:
        root = Path(temporary)
        private = root / ".compute-relay"
        private.mkdir(mode=0o700)
        (private / "project.key").write_text(secret); (private / "project.key").chmod(0o600)
        (private / "connection.json").write_text(json.dumps({"schema_version": "1.0", "broker_url": base_url, "workspace_root": str(root)}))
        (private / "connection.json").chmod(0o600)
        command = [sys.executable, "-m", "compute_relay.cli", "files", "finish", "--directory", str(root),
            "--investigation-id", group["id"], "--output", "results", "--acknowledge"]
        for extra in ([], ["--resume"]):
            result = subprocess.run(command + extra, capture_output=True, timeout=60)
            require(result.returncode == 0, "Installed files finish CLI failed")
        manifest_bytes = (root / "results/run-manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        require(manifest["summary"]["operationally_reconciled"] and manifest["summary"]["local_delivery_complete"], "Closeout did not reconcile and verify delivery")
        attempt = manifest["jobs"][0]["attempts"][0]
        require(attempt["delivery"]["acknowledged"] and attempt["local_delivery"]["state"] == "verified", "Verified results were not acknowledged")
        receipt = json.loads((root / attempt["local_delivery"]["receipt"]).read_bytes())
        artifact = receipt["files"][0]
        content = (root / attempt["local_delivery"]["receipt"]).parent.joinpath(artifact["path"]).read_bytes()
        require(content == b'{"answer":42}\n' and hashlib.sha256(content).hexdigest() == artifact["sha256"], "Exported answer/hash is incorrect")
        digest = hashlib.sha256(manifest_bytes).hexdigest()
    final = agent.request("GET", f"/api/v1/investigations/{group['id']}")
    allowance = agent.request("GET", f"/api/v1/projects/{project_id}/allowance")
    require(final["state"] == "closed" and final["summary"]["attempts"] == 1 and final["summary"]["unexecuted_packages"] == 0,
        "Closeout left abandoned work or started extra compute")
    require(final["summary"]["spent"] == allowance["spent"] and allowance["reserved"] == {"cpu_core_seconds": 0, "gpu_device_seconds": 0}, "Grouped accounting does not match the project ledger")
    print(json.dumps({"ok": True, "simulated": False, "attempts": 1, "cancelled_unused_packages": 2,
        "installed_cli_export_and_resume": True, "revision_wait": True, "manifest_sha256": digest,
        "spent": allowance["spent"], "reserved": allowance["reserved"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--demo-file", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--confirm-disposable-fixture", action="store_true")
    args = parser.parse_args()
    if not args.confirm_disposable_fixture or not 1 <= args.timeout <= 900:
        parser.error("Confirm the disposable fixture and use a timeout of 1 through 900 seconds")
    try:
        run(args.base_url, args.demo_file, args.timeout)
    except Exception as error:
        print(f"Investigation acceptance failed ({type(error).__name__}): " + (str(error) if isinstance(error, SmokeFailure) else "client/protocol error"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
