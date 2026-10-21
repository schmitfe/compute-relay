#!/usr/bin/env python3
"""Run the complete synthetic HTTP workflow in disposable native processes."""

import argparse
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from uuid import uuid4

from container_smoke import Client, require, run


def budgeted_workflow(base_url, demo_file):
    demo = json.loads(demo_file.read_text())
    agent = Client(base_url, token=(demo_file.parent / "agent-token").read_text().strip())
    descriptor = agent.request("POST", f"/api/v1/projects/{demo['project_id']}/objects", {
        "path": "main.py", "content_base64": base64.b64encode(b"print('budgeted synthetic workload')\n").decode("ascii")})
    request = {"schema_version": "1.0", "project_id": demo["project_id"], "connection_id": demo["connection_id"],
               "name": "budgeted-" + uuid4().hex[:12], "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
               "environment": {"id": demo["environment_id"], "revision_sha256": demo["environment_revision_sha256"]},
               "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
               "files": [descriptor], "outputs": [], "slurm": {"partition": "debug", "account": "broker"}}
    job = agent.request("POST", "/api/v1/jobs", request)
    agent.request("POST", f"/api/v1/jobs/{job['id']}/prepare", {})
    execution = agent.request("POST", f"/api/v1/jobs/{job['id']}/execution-requests", {})
    require(execution.get("attempt_id"), "Budgeted request did not authorize automatically")
    for _ in range(40):
        detail = agent.request("GET", f"/api/v1/jobs/{job['id']}")
        if detail["attempts"][0]["accounting_state"] == "settled":
            require(detail["attempts"][0]["state"] == "succeeded", "Budgeted job did not succeed")
            break
        time.sleep(1)
    else:
        raise RuntimeError("Budgeted fake execution did not settle")
    budget = agent.request("GET", f"/api/v1/projects/{demo['project_id']}/allowance")
    require(budget["reserved"]["cpu_core_seconds"] == 0 and budget["spent"]["cpu_core_seconds"] > 0,
            "Budgeted job did not settle its reservation")
    print(json.dumps({"ok": True, "backend": "fake", "simulated": True, "mode": "budgeted", "accounting_state": "settled"}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["approval_required", "budgeted"], default="approval_required")
    parser.add_argument("--management", action="store_true", help="Also exercise human project/key/cluster management over HTTP")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="compute-broker-smoke-") as directory:
        root = Path(directory)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = dict(os.environ, BROKER_DATA_DIR=str(root / "control"), BROKER_PORT=str(port),
                   BROKER_CONTROL_URL=f"http://127.0.0.1:{port}")
        # Do not let a developer's explicit data paths select a real installation.
        for key in ("BROKER_DB_PATH", "BROKER_ARTIFACT_ROOT", "BROKER_SECRET_KEY_FILE", "DJANGO_SETTINGS_MODULE"):
            env.pop(key, None)
        subprocess.run([sys.executable, "-m", "compute_broker.cli", "bootstrap-demo", "--output", str(root / "demo"),
                        "--fake-state-directory", str(root / "fake"), "--mode", args.mode], env=env, check=True, stdout=subprocess.DEVNULL)
        env.update(BROKER_WORKER_CONFIG=str(root / "demo/worker.json"), BROKER_WORKER_TOKEN_FILE=str(root / "demo/worker-token"),
                   BROKER_WORKER_JOURNAL=str(root / "journal"))
        processes = []
        with (root / "service.log").open("w+") as log:
            try:
                control = subprocess.Popen([sys.executable, "-m", "compute_broker.control"], env=env, stdout=log, stderr=log)
                processes.append(control)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    if control.poll() is not None:
                        raise RuntimeError("Disposable control process exited before startup")
                    try:
                        with opener.open(env["BROKER_CONTROL_URL"] + "/healthz", timeout=1) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        time.sleep(.1)
                else:
                    raise RuntimeError("Disposable control startup timed out")
                processes.append(subprocess.Popen([sys.executable, "-m", "compute_broker.worker"], env=env, stdout=log, stderr=log))
                if args.mode == "budgeted":
                    budgeted_workflow(env["BROKER_CONTROL_URL"], root / "demo/demo.json")
                else:
                    run(env["BROKER_CONTROL_URL"], root / "demo/demo.json", 60, management=args.management)
            finally:
                for process in processes:
                    process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    main()
