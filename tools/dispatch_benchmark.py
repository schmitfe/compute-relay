#!/usr/bin/env python3
"""Disposable Slurm-only throughput benchmark; never use institutional keys.

The child instruments the installed connector without changing production code.
Only durations, operation names and synthetic attempt IDs enter its private log.
"""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4

from container_smoke import Client, SmokeFailure, create_job, revealed_key, require
from personal_connector_smoke import finish_configuration, redirected_path


def connector_child(directory):
    from compute_relay.worker.personal_connector import read_private
    from compute_relay.transports.async_openssh import AsyncOpenSSHTransport
    from compute_relay.worker.async_client import AsyncJsonClient
    state = read_private(directory / "state.json")
    require(state["connection"]["ssh"]["host"] == "slurm-fixture" and state["broker_url"] == "http://control:8765",
        "Instrumentation requires the disposable fixture")

    def emit(event, **fields):
        print(json.dumps({"event": event, "at": time.monotonic(), **fields}), flush=True)

    original_remote, original_http = AsyncOpenSSHTransport.call_helper, AsyncJsonClient.request

    async def remote(self, source, request):
        started = time.monotonic()
        try:
            return await original_remote(self, source, request)
        finally:
            emit("remote", operation=request["operation"], started=started,
                seconds=time.monotonic() - started, helper_bytes=len(source.encode()))

    async def http(self, method, path, document=None, **kwargs):
        started = time.monotonic()
        result = await original_http(self, method, path, document, **kwargs)
        kind = path.rsplit("/", 1)[-1]
        emit("http", operation=kind, seconds=time.monotonic() - started)
        if kind == "report" and document:
            attempt = path.split("/")[-2]
            if document.get("outcome") == "submitted":
                emit("submitted", attempt=attempt)
            observation = document.get("observation", {})
            if observation.get("terminal") and observation.get("accounting", {}).get("final"):
                emit("settled", attempt=attempt)
        return result

    AsyncOpenSSHTransport.call_helper, AsyncJsonClient.request = remote, http
    from compute_relay.cli import main
    main(["connector", "--state-directory", str(directory), "--non-interactive", "--allow-insecure-http"])


def stop(worker):
    if worker.poll() is None:
        worker.terminate()
    worker.wait(timeout=20)


def read_events(log):
    events = []
    for line in log.read_text(errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and "event" in item:
            events.append(item)
    return events


def distribution(values):
    values = sorted(values)
    return {"count": len(values), "median_seconds": round(statistics.median(values), 4),
        "p95_seconds": round(values[math.ceil(len(values) * .95) - 1], 4)}


def summarize(events, started, jobs, parallelism):
    submitted = {e["attempt"]: e["at"] for e in events if e["event"] == "submitted"}
    settled = {e["attempt"]: e["at"] for e in events if e["event"] == "settled"}
    require(len(submitted) == len(settled) == jobs, "Benchmark lost a submission or final report")
    remote = [e for e in events if e["event"] == "remote"]
    edges = [(e["started"], 1) for e in remote] + [(e["at"], -1) for e in remote]
    active, peak = 0, 0
    for _, delta in sorted(edges):
        active += delta; peak = max(peak, active)
    dispatch = max(submitted.values()) - started
    return {"parallelism": parallelism, "jobs": jobs, "all_native_ids_seconds": round(dispatch, 3),
        "native_ids_per_second": round(jobs / dispatch, 3),
        "all_settled_seconds": round(max(settled.values()) - started, 3),
        "peak_remote_commands": peak,
        "remote_operations": {op: distribution([e["seconds"] for e in remote if e["operation"] == op])
            for op in sorted({e["operation"] for e in remote})},
        "helper_source_bytes_sent": sum(e["helper_bytes"] for e in remote),
        "http_requests": dict(Counter(e["operation"] for e in events if e["event"] == "http"))}


def prepare_project(human, demo, jobs, index):
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    human.request("POST", "/projects/new/", {"name": f"Dispatch benchmark {index}", "mode": "approval_required",
        "cpu_hours": "10", "gpu_hours": "0", "max_cpus": "1", "max_gpus": "0", "memory_mib": "128",
        "wall_minutes": "1", "max_active_jobs": str(jobs), "max_total_jobs": str(jobs), "expires_at": expiry,
        "connections": [demo["connection_id"]], "environments": [demo["environment_id"]]}, form=True, binary=True)
    path = redirected_path(human)
    require(path.startswith("/projects/") and path != "/projects/new/", "Benchmark project was not created")
    project = str(UUID(path.strip("/").split("/")[-1]))
    html = human.request("POST", f"/projects/{project}/keys/new/", {"name": "Synthetic benchmark",
        "expires_at": expiry, "cluster_access": "inherit", "execution_mode": "inherit", "submission_id": str(uuid4()),
        "scopes": ["jobs:read", "jobs:write", "jobs:execute", "objects:write", "artifacts:read"]}, form=True, binary=True)
    token, _ = revealed_key(human, html)
    agent = Client("http://control:8765", token=token)
    source = b'import time\nfrom pathlib import Path\ntime.sleep(0.05)\nPath("result.json").write_text("{}")\n'
    proposals = [create_job(agent, {**demo, "project_id": project}, "slurm", source, f"benchmark-{index}-{i}") for i in range(jobs)]
    for job_id, digest, execution_id in proposals:
        human.request("POST", f"/api/v1/jobs/{job_id}/decisions", {"execution_request_id": execution_id,
            "manifest_sha256": digest, "approved": True, "comment": "Disposable benchmark only."})
    return agent, project, [job_id for job_id, _, _ in proposals]


def run(args):
    demo = json.loads(args.demo_file.read_bytes())
    require(demo.get("backend") == "slurm" and demo.get("cluster_form", {}).get("host") == "slurm-fixture",
        "Benchmark requires the disposable Slurm fixture")
    human = Client("http://control:8765"); human.login(demo["owner_username"], demo["owner_password"])
    page = human.request("GET", f"/clusters/{demo['connection_id']}/", binary=True)
    revision = re.search(rb'name="setup_revision" value="([0-9]+)"', page)
    setup = human.request("POST", f"/clusters/{demo['connection_id']}/connect/",
        {"setup_revision": revision.group(1).decode()} if revision else {}, form=True)
    setup["local"] = {"identity_file": "/run/fixture-ssh/id_ed25519", "known_hosts": "/run/fixture-ssh/known_hosts", "session_seconds": 300}
    with tempfile.TemporaryDirectory(prefix="relay-bench-", dir="/tmp") as temporary:
        root = Path(temporary); root.chmod(0o700)
        path = root / "setup.json"; path.write_text(json.dumps(setup)); path.chmod(0o600)
        directory = root / "worker"
        log = root / "setup.log"
        with log.open("wb") as output:
            worker = subprocess.Popen([sys.executable, "-m", "compute_relay.cli", "connector", "--state-directory", str(directory),
                "--setup", str(path), "--non-interactive", "--allow-insecure-http"], stdin=subprocess.DEVNULL, stdout=output, stderr=output)
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    require(worker.poll() is None, "Benchmark connector stopped during setup")
                    if (directory / "state.json").exists() and json.loads((directory / "state.json").read_bytes()).get("defaults_initialized"):
                        break
                    time.sleep(.2)
                else:
                    raise SmokeFailure("Benchmark setup timed out")
                finish_configuration(human, demo, directory)
            finally:
                stop(worker)
        reports = []
        for index, parallelism in enumerate(args.parallelism):
            agent, project, job_ids = prepare_project(human, demo, args.jobs, index)
            log = root / f"phase-{index}.log"
            with log.open("wb") as output:
                started = time.monotonic()
                worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--connector-state", str(directory)],
                    env={**os.environ, "COMPUTE_RELAY_MAX_REMOTE_OPERATIONS": str(parallelism)},
                    stdin=subprocess.DEVNULL, stdout=output, stderr=output)
                try:
                    deadline = started + 240
                    while time.monotonic() < deadline:
                        require(worker.poll() is None, "Instrumented connector stopped")
                        events = read_events(log)
                        if len({e["attempt"] for e in events if e["event"] == "settled"}) == args.jobs:
                            break
                        time.sleep(.1)
                    else:
                        raise SmokeFailure("Benchmark did not settle before its deadline")
                finally:
                    stop(worker)
            budget = agent.request("GET", f"/api/v1/projects/{project}/allowance")
            require(budget["reserved"] == {"cpu_core_seconds": 0, "gpu_device_seconds": 0}, "Benchmark retained reservations")
            require(all(agent.request("GET", f"/api/v1/jobs/{job_id}")["state"] in {"succeeded", "completed"}
                for job_id in job_ids), "A benchmark job did not succeed")
            report = summarize(read_events(log), started, args.jobs, parallelism)
            reports.append(report)
            print(json.dumps({"phase": index, **report}), flush=True)
        print(json.dumps({"ok": True, "fixture_only": True, "job_sleep_seconds": .05, "fixture_cpus": 2, "phases": reports}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-file", type=Path, default=Path("/run/demo/demo.json"))
    parser.add_argument("--jobs", type=int, choices=range(1, 101), default=24, metavar="1..100")
    parser.add_argument("--parallelism", type=int, nargs="+", choices=range(1, 9), default=[1, 4, 4, 1])
    parser.add_argument("--connector-state", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        connector_child(args.connector_state) if args.connector_state else run(args)
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error) if isinstance(error, SmokeFailure) else type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
