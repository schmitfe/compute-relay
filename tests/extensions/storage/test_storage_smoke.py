from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest

from storage_probe_smoke import SmokeFailure, fresh_observation, options, validate_cluster_page, validate_demo


def demo():
    return {"backend": "slurm", "connection_id": str(uuid4()), "environment_id": str(uuid4()),
            "cluster_form": {"host": "slurm-fixture", "user": "brokerjob", "port": 22, "remote_root": "/home/brokerjob/jobs"}}


@pytest.mark.parametrize("field,value", [("host", "university.example"), ("user", "someone"), ("port", 2222), ("remote_root", "/other")])
def test_fixture_client_refuses_nonfixture_targets(field, value):
    fixture = demo()
    validate_demo(fixture)
    fixture["cluster_form"][field] = value
    with pytest.raises(SmokeFailure, match="disposable Slurm fixture"):
        validate_demo(fixture)


def test_fixture_client_checks_actual_cluster_identity_and_never_accepts_fake():
    validate_cluster_page("<p>slurm · brokerjob@slurm-fixture · Discovery: <strong>ready</strong></p>".encode())
    with pytest.raises(SmokeFailure, match="not the disposable"):
        validate_cluster_page(b"<p>slurm-fixture name; actual cluster researcher@university.example</p>")
    fixture = demo()
    fixture["backend"] = "fake"
    with pytest.raises(SmokeFailure):
        validate_demo(fixture)


def test_fixture_client_parses_only_named_nonempty_location_choices():
    page = b'<select name="unrelated"><option value="x">Elsewhere</option></select><select name="results"><option value="">---</option><option value="fixture">Fixture &amp; root</option></select>'
    assert options(page, "results") == [{"value": "fixture", "text": "Fixture & root"}]


def test_fixture_acceptance_requires_new_real_metadata_with_native_quota_unknown():
    started = datetime.now(timezone.utc)
    data = {"source": "statvfs", "status": "available", "observed_at": (started + timedelta(milliseconds=1)).isoformat(),
            "identity_verified": False, "total_bytes": 1000, "available_bytes": 500, "total_inodes": 100,
            "available_inodes": 50, "quota": {"status": "unknown"}}
    snapshot = {"pools": [{"id": "fixture", "probe_enabled": True, "probe_supported": True, "observation": data}]}
    assert fresh_observation(snapshot, "fixture", started) == data
    data["observed_at"] = (started - timedelta(seconds=1)).isoformat()
    assert fresh_observation(snapshot, "fixture", started) is None
    data["observed_at"] = (started + timedelta(milliseconds=1)).isoformat()
    data["quota"]["status"] = "reported"
    with pytest.raises(SmokeFailure, match="unknown native quota"):
        fresh_observation(snapshot, "fixture", started)


def test_compose_overlay_enables_only_storage_and_mounts_just_the_client_read_only():
    root = Path(__file__).resolve().parents[3]
    services = json.loads((root / "deploy/compose.storage-test.yml").read_text())["services"]
    assert services["control"]["environment"] == {"BROKER_EXTENSIONS": "storage"}
    assert services["worker"]["environment"] == {"BROKER_EXTENSIONS": "storage"}
    smoke = services["smoke"]
    assert smoke["entrypoint"] == ["python", "/opt/storage_probe_smoke.py"]
    assert "--confirm-disposable-fixture" in smoke["command"]
    assert smoke["command"][-2:] == ["--timeout", "120"]
    assert smoke["volumes"] == [{"type": "bind", "source": "../tools/storage_probe_smoke.py",
                                  "target": "/opt/storage_probe_smoke.py", "read_only": True}]
