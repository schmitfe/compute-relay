import hashlib
from uuid import uuid4

import pytest

from workflow_smoke import SmokeFailure, validate_demo, verify_artifact, verify_edge


def fixture_demo():
    return {"backend": "slurm", "project_id": str(uuid4()), "connection_id": str(uuid4()), "environment_id": str(uuid4()),
        "cluster_form": {"host": "slurm-fixture", "user": "brokerjob", "port": 22, "remote_root": "/home/brokerjob/jobs"}}


@pytest.mark.parametrize("backend", ["fake", "rest", "slurm"])
def test_workflow_smoke_requires_explicit_native_fixture(backend):
    demo = fixture_demo();demo["backend"] = backend
    if backend == "slurm":
        validate_demo(demo)
        demo["cluster_form"]["host"] = "institution.example"
    with pytest.raises(SmokeFailure):
        validate_demo(demo)


def test_workflow_artifact_verifier_requires_exact_bytes_and_hash():
    expected = b"[2,4,6]\n"
    descriptor = {"sha256": hashlib.sha256(expected).hexdigest()}
    verify_artifact(expected, descriptor, expected)
    with pytest.raises(SmokeFailure):
        verify_artifact(expected + b" ", descriptor, expected)
    with pytest.raises(SmokeFailure):
        verify_artifact(expected, {"sha256": "a" * 64}, expected)


@pytest.mark.parametrize("change", ["attempt", "artifact", "hash", "package"])
def test_workflow_verifier_binds_consumer_to_exact_producer_attempt_and_artifact(change):
    artifact = {"id": "artifact-id", "sha256": "b" * 64, "size_bytes": 8}
    node = {"inputs": [{"artifact_id": artifact["id"], "sha256": artifact["sha256"], "producer_attempt_id": "attempt-id", "path": "inputs/data.json"}]}
    job = {"manifest": {"request": {"files": [{"path": "inputs/data.json", "sha256": artifact["sha256"], "size_bytes": artifact["size_bytes"]}]}}}
    verify_edge(node, job, artifact, "attempt-id", "inputs/data.json")
    if change == "attempt": node["inputs"][0]["producer_attempt_id"] = "other-attempt"
    elif change == "artifact": node["inputs"][0]["artifact_id"] = "other-artifact"
    elif change == "hash": node["inputs"][0]["sha256"] = "c" * 64
    else: job["manifest"]["request"]["files"][0]["sha256"] = "c" * 64
    with pytest.raises(SmokeFailure):
        verify_edge(node, job, artifact, "attempt-id", "inputs/data.json")
