#!/usr/bin/env python3
"""Opt-in empty-venv build and reuse on the disposable Docker Slurm fixture.

Uses only public HTTP and explicit synthetic demo credentials. Installers and the
built interpreter run exclusively as approved compute jobs, never in this client.
"""

from __future__ import annotations

import argparse
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

from container_smoke import Client, SmokeFailure, create_job, require, revealed_key


def validate_demo(demo):
    target = demo.get("cluster_form", {})
    require(demo.get("backend") == "slurm" and target.get("host") == "slurm-fixture"
            and target.get("user") == "brokerjob" and target.get("port") == 22
            and target.get("remote_root") == "/home/brokerjob/jobs",
            "Environment acceptance requires the explicit disposable Slurm fixture metadata")
    for field in ("project_id", "connection_id", "environment_id"):
        require(str(UUID(demo[field])) == demo[field], "Fixture metadata contains a noncanonical identifier")


def approve(human, job_id, digest, execution):
    require(execution.get("state") == "awaiting_approval" and execution.get("attempt_id") is None,
            "Environment acceptance did not wait for human review")
    result = human.request("POST", f"/api/v1/jobs/{job_id}/decisions", {
        "execution_request_id": execution["id"], "manifest_sha256": digest, "approved": True,
        "comment": "Approve this immutable disposable environment acceptance package once.",
    })
    require(result.get("attempt_id"), "Human approval did not create an attempt")
    return result["attempt_id"]


def wait_job(agent, job_id, attempt_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = agent.request("GET", f"/api/v1/jobs/{job_id}")
        attempt = next((item for item in job["attempts"] if item["id"] == attempt_id), None)
        require(attempt is not None, "Expected disposable attempt disappeared")
        require(attempt["state"] not in {"failed", "cancelled", "expired"}, "Disposable job failed before acceptance")
        if attempt["state"] == "succeeded" and attempt["accounting_state"] == "settled":
            return attempt
        time.sleep(1)
    raise SmokeFailure("Disposable job did not complete final accounting before the deadline")


def wait_build(agent, build_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = agent.request("GET", f"/api/v1/environment-builds/{build_id}")
        require(result["state"] not in {"failed", "cancelled", "expired", "simulation_only", "finalization_failed"},
                "Environment build did not produce a verified real prefix")
        if result["state"] == "ready":
            require(result.get("receipt", {}).get("verified") is True, "Ready environment lacks verified metadata")
            return result
        time.sleep(1)
    raise SmokeFailure("Environment publication did not finish before the deadline")


def verify_result(content, descriptor, prefix):
    require(hashlib.sha256(content).hexdigest() == descriptor["sha256"], "Reused-environment artifact hash mismatch")
    data = json.loads(content)
    require(data.get("ok") is True and data.get("prefix") == prefix and data.get("base_prefix") != prefix,
            "Second job did not use the published virtual environment")


def run(base_url, demo_file, timeout, *, platform_tag="linux-x86_64", python_major_minor="3.11", module_bootstrap=False):
    demo = json.loads(demo_file.read_bytes())
    validate_demo(demo)
    human = Client(base_url)
    human.login(demo["owner_username"], demo["owner_password"])
    suffix = uuid4().hex[:12]
    if module_bootstrap:
        human.request("POST", f"/clusters/{demo['connection_id']}/environments/new/", {
            "name": "Fixture module bootstrap " + suffix, "kind": "modules", "interpreter": "python3",
            "modules": "fixture-python/1.0", "module_init": "", "module_paths": "/opt/fixture/modulefiles",
        }, form=True, binary=True)
        page = human.request("GET", f"/clusters/{demo['connection_id']}/test/", binary=True)
        match = re.search(rb'<option value="([0-9a-f-]{36})">Fixture module bootstrap ' + suffix.encode() + rb'</option>', page)
        require(match is not None, "Module Python bootstrap was not created")
        demo["environment_id"] = match.group(1).decode()
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    project_fields = {"name": "Disposable environment acceptance " + suffix, "mode": "approval_required",
        "cpu_hours": "2", "gpu_hours": "0", "max_cpus": "1", "max_gpus": "0", "memory_mib": "512",
        "wall_minutes": "1", "charge_minutes": "2", "max_active_jobs": "2", "max_total_jobs": "2",
        "expires_at": expiry, "connections": [demo["connection_id"]], "environments": [demo["environment_id"]],
        "allowed_gpu_types": ""}
    human.request("POST", "/projects/new/", project_fields, form=True, binary=True)
    match = re.fullmatch(r"/projects/([0-9a-f-]{36})/", urllib.parse.urlsplit(human.last_url).path)
    require(match is not None, "Disposable environment project creation failed")
    project_id = str(UUID(match.group(1)))
    scopes = ["environments:build", "objects:write", "jobs:read", "jobs:write", "jobs:execute", "jobs:cancel", "artifacts:read"]
    html = human.request("POST", f"/projects/{project_id}/keys/new/", {"name": "Disposable environment builder",
        "expires_at": expiry, "scopes": scopes, "cluster_access": "inherit", "allowed_connections": [],
        "execution_mode": "approval_required", "submission_id": str(uuid4())}, form=True, binary=True)
    secret, _ = revealed_key(human, html)
    agent = Client(base_url, token=secret)
    capabilities = agent.request("GET", "/api/v1/capabilities")
    require("environments" in capabilities.get("enabled_extensions", []), "Environment extension is not enabled on control")
    inventory = agent.request("GET", "/api/v1/connections")["connections"]
    connection = next((item for item in inventory if item["id"] == demo["connection_id"]), None)
    require(connection is not None and connection.get("backend") == "slurm", "Disposable native connection is unavailable")
    environments = agent.request("GET", "/api/v1/connections")["environments"]
    base = next(item for item in environments if item["id"] == demo["environment_id"])
    demo["environment_revision_sha256"] = base["revision_sha256"]
    policy = {"enabled": True, "allowed_base_environment_ids": [demo["environment_id"]], "allowed_managers": ["venv"],
        "allowed_sources": ["uploaded_wheels"], "resource_limits": {"cpus": 1, "memory_mib": 512, "time_limit_seconds": 60},
        "platform_tag": platform_tag, "python_major_minor": python_major_minor, "max_environment_bytes": 1073741824,
        "max_environment_files": 20000, "minimum_free_bytes": 10485760, "minimum_free_files": 100}
    human.request("PUT", f"/api/v1/projects/{project_id}/environment-build-policy", policy)
    recipe = {"name": "empty-stdlib-" + suffix, "connection_id": demo["connection_id"], "manager": "venv",
        "base_environment": {"id": demo["environment_id"], "revision_sha256": demo["environment_revision_sha256"]},
        "requirements": [], "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 512, "time_limit_seconds": 60},
        "slurm": {"partition": "debug", "account": "broker"}, "estimated_bytes": 50000000, "estimated_files": 100}
    build = agent.request("POST", f"/api/v1/projects/{project_id}/environment-builds", recipe)
    prepared = agent.request("POST", f"/api/v1/environment-builds/{build['id']}/prepare", {})
    job = agent.request("GET", f"/api/v1/jobs/{prepared['job_id']}")
    require(job["manifest"].get("extension_name") == "environments", "Build package lost its extension binding")
    execution = agent.request("POST", f"/api/v1/environment-builds/{build['id']}/execution-requests", {})
    attempt_id = approve(human, job["id"], job["manifest_sha256"], execution)
    first_attempt = wait_job(agent, job["id"], attempt_id, timeout)
    ready = wait_build(agent, build["id"], timeout)
    require(ready["environment"]["permitted_for_jobs"] is False, "Publication silently expanded execution permission")
    agent.request("POST", f"/api/v1/environment-builds/{build['id']}/promote", {}, expected=(403,), binary=True)
    promoted = human.request("POST", f"/api/v1/environment-builds/{build['id']}/promote", {})
    require(promoted["environment"]["permitted_for_jobs"] is True, "Explicit human promotion did not update the grant")
    prefix = ready["receipt"]["prefix"]
    module_check = "import os\nassert 'fixture-python/1.0' in os.environ.get('LOADEDMODULES','').split(':')\n" if module_bootstrap else ""
    source = ("import json,sys\nfrom pathlib import Path\n" + module_check +
        f"assert sys.prefix == {prefix!r}\nassert sys.base_prefix != sys.prefix\n"
        "Path('result.json').write_text(json.dumps({'ok':True,'prefix':sys.prefix,'base_prefix':sys.base_prefix}))\n"
        "print('BROKER_ENVIRONMENT_REUSE_OK',flush=True)\n").encode()
    selected = {**demo, "project_id": project_id, "environment_id": promoted["environment"]["id"],
        "environment_revision_sha256": promoted["environment"]["revision_sha256"]}
    second_job, digest, execution_id = create_job(agent, selected, "slurm", source, "reuse-venv-" + suffix)
    second_attempt_id = approve(human, second_job, digest, {"id": execution_id, "state": "awaiting_approval", "attempt_id": None})
    second_attempt = wait_job(agent, second_job, second_attempt_id, timeout)
    logs = agent.request("GET", f"/api/v1/attempts/{second_attempt_id}/logs")
    require("BROKER_ENVIRONMENT_REUSE_OK" in logs["stdout"], "Reused environment did not produce its output marker")
    artifacts = agent.request("GET", f"/api/v1/attempts/{second_attempt_id}/artifacts")["artifacts"]
    result = next((item for item in artifacts if item["path"] == "result.json"), None)
    require(result is not None, "Reused-environment result artifact is missing")
    verify_result(agent.request("GET", f"/api/v1/artifacts/{result['id']}/content", binary=True), result, prefix)
    cached = agent.request("POST", f"/api/v1/projects/{project_id}/environment-builds", recipe)
    require(cached["cache_hit"] is True and cached["id"] == ready["id"], "Verified recipe cache did not reuse the original prefix")
    allowance = agent.request("GET", f"/api/v1/projects/{project_id}/allowance")
    require(allowance["reserved"] == {"cpu_core_seconds": 0, "gpu_device_seconds": 0}
            and allowance["spent"]["cpu_core_seconds"] > 0 and allowance["jobs"]["admitted"] == 2,
            "Build and reuse did not share normal compute accounting and two submission slots")
    for attempt in (first_attempt, second_attempt):
        require(str(attempt.get("native_identity", {}).get("job_id", "")).isdigit(), "Missing native Slurm job identity")
    print(json.dumps({"ok": True, "simulated": False, "project_id": project_id, "build_id": ready["id"],
        "environment_id": promoted["environment"]["id"], "build_attempt_id": attempt_id,
        "reuse_attempt_id": second_attempt_id, "admitted_jobs": allowance["jobs"]["admitted"],
        "spent": allowance["spent"], "reserved": allowance["reserved"], "cache_hit": True, "module_bootstrap": module_bootstrap}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--demo-file", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--fixture-platform", default="linux-x86_64")
    parser.add_argument("--fixture-python-major-minor", default="3.11")
    parser.add_argument("--module-bootstrap", action="store_true", help="Build and reuse using the fixture Python module")
    parser.add_argument("--confirm-disposable-fixture", action="store_true")
    args = parser.parse_args()
    if not args.confirm_disposable_fixture:
        parser.error("Supply --confirm-disposable-fixture; this runs two approved compute jobs on the disposable fixture")
    if not 1 <= args.timeout <= 900:
        parser.error("timeout must be from 1 through 900 seconds")
    try:
        run(args.base_url, args.demo_file, args.timeout, platform_tag=args.fixture_platform,
            python_major_minor=args.fixture_python_major_minor, module_bootstrap=args.module_bootstrap)
    except SmokeFailure as error:
        print("Environment smoke failed: " + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as error:
        print(f"Environment smoke failed: client/protocol error ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
