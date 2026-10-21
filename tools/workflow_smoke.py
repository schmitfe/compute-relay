#!/usr/bin/env python3
"""Opt-in three-node artifact workflow on the disposable Docker Slurm fixture."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
from uuid import UUID, uuid4

from container_smoke import Client, SmokeFailure, require, revealed_key


def validate_demo(demo):
    target = demo.get("cluster_form", {})
    require(demo.get("backend") == "slurm" and target.get("host") == "slurm-fixture"
            and target.get("user") == "brokerjob" and target.get("port") == 22
            and target.get("remote_root") == "/home/brokerjob/jobs",
            "Workflow acceptance requires explicit disposable Slurm fixture metadata")
    for key in ("project_id", "connection_id", "environment_id"):
        require(str(UUID(demo[key])) == demo[key], "Fixture metadata contains an invalid identifier")


def verify_artifact(content, descriptor, expected):
    require(content == expected and hashlib.sha256(content).hexdigest() == descriptor["sha256"],
            "Workflow artifact bytes/hash differ from the expected result")


def verify_edge(node, job, artifact, producer_attempt, destination):
    require(node.get("inputs") == [{"artifact_id": artifact["id"], "sha256": artifact["sha256"],
        "producer_attempt_id": producer_attempt, "path": destination}], "Workflow edge lost exact artifact/attempt provenance")
    files = [item for item in job["manifest"]["request"]["files"] if item["path"] == destination]
    require(len(files) == 1 and files[0]["sha256"] == artifact["sha256"] and files[0]["size_bytes"] == artifact["size_bytes"],
            "Consumer package does not bind the exact producer artifact")


def node_map(run):
    return {item["key"]: item for item in run["nodes"]}


def stable_advance(agent, run_id, expected):
    """Two independent calls must retain every existing node execution identity."""
    latest = None
    for _ in range(2):
        latest = agent.request("POST", f"/api/v1/workflow-runs/{run_id}/advance", {})
        nodes = node_map(latest)
        for key, identity in expected.items():
            require((nodes[key]["job_id"], nodes[key]["execution_request_id"]) == identity,
                    "Repeated workflow advance changed a materialized node identity")
    return latest


def wait_ready(agent, run_id, key, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = agent.request("POST", f"/api/v1/workflow-runs/{run_id}/advance", {})
        node = node_map(run)[key]
        require(not node.get("error_code") and node["state"] not in {"failed", "cancelled", "rejected", "blocked_upstream", "blocked_artifact", "blocked_policy"},
                "Workflow node became blocked or failed before review")
        if node["job_id"] and node["execution_request_id"] and node["state"] == "awaiting_approval":
            return run, node
        time.sleep(1)
    raise SmokeFailure("Workflow node did not reach its own human review before the deadline")


def approve_and_wait(agent, human, node, job, timeout):
    require(not job["attempts"] and len(job["execution_requests"]) == 1, "Node executed early or has duplicate execution requests")
    result = human.request("POST", f"/api/v1/jobs/{job['id']}/decisions", {
        "execution_request_id": node["execution_request_id"], "manifest_sha256": job["manifest_sha256"],
        "approved": True, "comment": "Approve this immutable disposable workflow node once.",
    })
    attempt_id = result.get("attempt_id")
    require(attempt_id, "Workflow node approval did not create an attempt")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = agent.request("GET", f"/api/v1/jobs/{job['id']}")
        require(len(current["attempts"]) == 1 and len(current["execution_requests"]) == 1,
                "Workflow created a duplicate execution or attempt")
        attempt = current["attempts"][0]
        require(attempt["id"] == attempt_id and attempt["state"] not in {"failed", "cancelled", "expired"},
                "Workflow node failed or changed its admitted attempt")
        if attempt["state"] == "succeeded" and attempt["accounting_state"] == "settled":
            require(str(attempt.get("native_identity", {}).get("job_id", "")).isdigit(), "Workflow node lacks a native Slurm identity")
            return attempt
        time.sleep(1)
    raise SmokeFailure("Workflow node did not settle final accounting before the deadline")


def run(base_url, demo_file, timeout):
    demo = json.loads(demo_file.read_bytes())
    validate_demo(demo)
    human = Client(base_url)
    human.login(demo["owner_username"], demo["owner_password"])
    suffix = uuid4().hex[:12]
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    human.request("POST", "/projects/new/", {"name": "Disposable artifact workflow " + suffix, "mode": "approval_required",
        "cpu_hours": "2", "gpu_hours": "0", "max_cpus": "1", "max_gpus": "0", "memory_mib": "128",
        "wall_minutes": "1", "charge_minutes": "2", "max_active_jobs": "2", "max_total_jobs": "3", "expires_at": expiry,
        "connections": [demo["connection_id"]], "environments": [demo["environment_id"]], "allowed_gpu_types": ""}, form=True, binary=True)
    match = re.fullmatch(r"/projects/([0-9a-f-]{36})/", urllib.parse.urlsplit(human.last_url).path)
    require(match is not None, "Workflow project creation failed")
    project_id = str(UUID(match.group(1)))
    page = human.request("POST", f"/projects/{project_id}/keys/new/", {"name": "Disposable workflow agent", "expires_at": expiry,
        "scopes": ["workflows:write", "objects:write", "jobs:write", "jobs:read", "jobs:execute", "jobs:cancel", "artifacts:read"],
        "cluster_access": "inherit", "allowed_connections": [], "execution_mode": "approval_required", "submission_id": str(uuid4())}, form=True, binary=True)
    secret, _ = revealed_key(human, page)
    agent = Client(base_url, token=secret)
    require("workflows" in agent.request("GET", "/api/v1/capabilities").get("enabled_extensions", []),
            "Workflows are not enabled on this control service")
    inventory = agent.request("GET", "/api/v1/connections")["connections"]
    require(any(item["id"] == demo["connection_id"] and item["backend"] == "slurm" for item in inventory),
            "Disposable native workflow connection is unavailable")
    rows = [
        {"key": "produce", "output": "numbers.json", "expected": b"[1,2,3]\n", "source":
            b"from pathlib import Path\nPath('numbers.json').write_bytes(b'[1,2,3]\\n')\n"},
        {"key": "double", "output": "doubled.json", "expected": b"[2,4,6]\n", "input": "inputs/numbers.json", "producer": "produce", "source":
            b"import json\nfrom pathlib import Path\nvalues=json.loads(Path('inputs/numbers.json').read_bytes())\nPath('doubled.json').write_text(json.dumps([2*x for x in values],separators=(',',':'))+'\\n')\n"},
        {"key": "sum", "output": "result.json", "expected": b'{"sum":12}\n', "input": "inputs/doubled.json", "producer": "double", "source":
            b"import json\nfrom pathlib import Path\nvalues=json.loads(Path('inputs/doubled.json').read_bytes())\nPath('result.json').write_text(json.dumps({'sum':sum(values)},separators=(',',':'))+'\\n')\n"},
    ]
    graph = {"name": "artifact-chain-" + suffix, "nodes": []}
    by_key = {row["key"]: row for row in rows}
    for row in rows:
        descriptor = agent.request("POST", f"/api/v1/projects/{project_id}/objects", {
            "path": "main.py", "content_base64": base64.b64encode(row["source"]).decode()})
        require(descriptor["sha256"] == hashlib.sha256(row["source"]).hexdigest(), "Workflow source upload hash mismatch")
        request = {"schema_version": "1.0", "project_id": project_id, "connection_id": demo["connection_id"], "name": row["key"] + "-" + suffix,
            "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
            "environment": {"id": demo["environment_id"], "revision_sha256": demo["environment_revision_sha256"]},
            "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
            "slurm": {"partition": "debug", "account": "broker"}, "files": [descriptor], "outputs": [row["output"]]}
        node = {"key": row["key"], "request": request}
        if row.get("producer"):
            node["inputs"] = [{"node": row["producer"], "output": by_key[row["producer"]]["output"], "path": row["input"]}]
        graph["nodes"].append(node)
    revision = agent.request("POST", f"/api/v1/projects/{project_id}/workflow-revisions", graph)
    started = agent.request("POST", f"/api/v1/workflow-revisions/{revision['id']}/runs", {})
    run_id = started["id"]
    identities, results = {}, {}
    for position, row in enumerate(rows):
        status, node = wait_ready(agent, run_id, row["key"], timeout)
        identities[row["key"]] = (node["job_id"], node["execution_request_id"])
        repeated = stable_advance(agent, run_id, identities)
        require(all(not node_map(repeated)[later["key"]]["job_id"] for later in rows[position + 1:]),
                "A dependent workflow node materialized before its producer's successful final accounting")
        job = agent.request("GET", f"/api/v1/jobs/{node['job_id']}")
        require(job["manifest"].get("extension_name") == "workflows", "Workflow node lost its immutable extension binding")
        if row.get("producer"):
            producer = results[row["producer"]]
            verify_edge(node, job, producer["artifact"], producer["attempt"]["id"], row["input"])
        attempt = approve_and_wait(agent, human, node, job, timeout)
        artifacts = agent.request("GET", f"/api/v1/attempts/{attempt['id']}/artifacts")["artifacts"]
        artifact = next((item for item in artifacts if item["path"] == row["output"]), None)
        require(artifact is not None, "Workflow producer's declared artifact is missing")
        content = agent.request("GET", f"/api/v1/artifacts/{artifact['id']}/content", binary=True)
        verify_artifact(content, artifact, row["expected"])
        results[row["key"]] = {"attempt": attempt, "artifact": artifact}
    final = stable_advance(agent, run_id, identities)
    require(final["state"] == "succeeded" and len(final["nodes"]) == 3, "Workflow did not finish exactly three successful nodes")
    for row in rows:
        job = agent.request("GET", f"/api/v1/jobs/{identities[row['key']][0]}")
        require(len(job["execution_requests"]) == 1 and len(job["attempts"]) == 1, "Finished workflow gained duplicate execution")
    allowance = agent.request("GET", f"/api/v1/projects/{project_id}/allowance")
    require(allowance["jobs"]["admitted"] == 3 and allowance["jobs"]["remaining"] == 0
            and allowance["reserved"] == {"cpu_core_seconds": 0, "gpu_device_seconds": 0}
            and allowance["spent"]["cpu_core_seconds"] > 0, "Workflow did not share ordinary final accounting and submission caps")
    print(json.dumps({"ok": True, "simulated": False, "project_id": project_id, "workflow_run_id": run_id,
        "node_count": 3, "per_node_reviews": 3, "admitted_jobs": allowance["jobs"]["admitted"],
        "artifact_sha256": {key: result["artifact"]["sha256"] for key, result in results.items()},
        "spent": allowance["spent"], "reserved": allowance["reserved"], "duplicate_advance_checked": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--demo-file", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--confirm-disposable-fixture", action="store_true")
    args = parser.parse_args()
    if not args.confirm_disposable_fixture:
        parser.error("Supply --confirm-disposable-fixture; this runs three separately approved jobs on the disposable fixture")
    if not 1 <= args.timeout <= 900:
        parser.error("timeout must be from 1 through 900 seconds")
    try:
        run(args.base_url, args.demo_file, args.timeout)
    except SmokeFailure as error:
        print("Workflow smoke failed: " + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as error:
        print(f"Workflow smoke failed: client/protocol error ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
