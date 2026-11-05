import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from environment_build_smoke import SmokeFailure, validate_demo, verify_result, wait_job


def fixture_demo():
    return {"backend": "slurm", "project_id": str(uuid4()), "connection_id": str(uuid4()), "environment_id": str(uuid4()),
        "cluster_form": {"host": "slurm-fixture", "user": "brokerjob", "port": 22, "remote_root": "/home/brokerjob/jobs"}}


@pytest.mark.parametrize("change", ["fake", "host", "user", "root", "port"])
def test_acceptance_client_refuses_nonfixture_or_simulated_targets(change):
    demo = fixture_demo()
    validate_demo(demo)
    if change == "fake": demo["backend"] = "fake"
    elif change == "host": demo["cluster_form"]["host"] = "real-university.example"
    elif change == "user": demo["cluster_form"]["user"] = "someone"
    elif change == "root": demo["cluster_form"]["remote_root"] = "/elsewhere"
    else: demo["cluster_form"]["port"] = 2222
    with pytest.raises(SmokeFailure, match="disposable Slurm fixture"):
        validate_demo(demo)


def test_reuse_requires_correct_result_hash_and_real_venv_prefix():
    prefix = "/home/brokerjob/jobs/attempt/environment"
    content = json.dumps({"ok": True, "prefix": prefix, "base_prefix": "/usr"}).encode()
    descriptor = {"sha256": hashlib.sha256(content).hexdigest()}
    verify_result(content, descriptor, prefix)
    with pytest.raises(SmokeFailure, match="hash mismatch"):
        verify_result(content + b" ", descriptor, prefix)
    with pytest.raises(SmokeFailure, match="did not use"):
        verify_result(content, descriptor, "/different/environment")


def test_environment_acceptance_waits_for_outputs_after_early_settlement(monkeypatch):
    monkeypatch.setattr("environment_build_smoke.time.sleep", lambda _: None)
    snapshots = iter({"attempts": [{"id": "attempt", "state": "succeeded", "accounting_state": "settled",
        "collection_state": state}]} for state in ("pending", "complete"))
    agent = SimpleNamespace(request=lambda *args: next(snapshots))
    assert wait_job(agent, "job", "attempt", 2)["collection_state"] == "complete"
