from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest

import storage_probe_smoke
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
    validate_cluster_page("<p>Discovery: <strong>ready</strong></p><p>slurm · brokerjob@slurm-fixture</p>".encode())
    validate_cluster_page(b"<p>\n slurm &middot; <code>brokerjob@slurm-fixture</code> </p>")
    with pytest.raises(SmokeFailure, match="not the disposable"):
        validate_cluster_page(b"<p>slurm-fixture name; actual cluster researcher@university.example</p>")
    fixture = demo()
    fixture["backend"] = "fake"
    with pytest.raises(SmokeFailure):
        validate_demo(fixture)


@pytest.mark.parametrize("page", [
    "<p>slurm · brokerjob@slurm-fixture.example</p>",
    "<p>slurm · researcher@slurm-fixture</p>",
    "<p>fake · brokerjob@slurm-fixture</p>",
    "<p>Selected university cluster; example: slurm · brokerjob@slurm-fixture</p>",
    "<!-- <p>slurm · brokerjob@slurm-fixture</p> --><p>slurm · researcher@university.example</p>",
    '<script>"<p>slurm · brokerjob@slurm-fixture</p>"</script>',
    '<p><script>slurm · brokerjob@slurm-fixture</script></p>',
    '<p><style>slurm · brokerjob@slurm-fixture</style></p>',
])
def test_fixture_client_rejects_similar_or_incidental_identity_text(page):
    with pytest.raises(SmokeFailure, match="not the disposable"):
        validate_cluster_page(page.encode())


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


@pytest.mark.parametrize("inventory_matches", [True, False])
def test_fixture_run_enrolls_and_controls_probes_with_explicit_project_scope(tmp_path, monkeypatch, capsys, inventory_matches):
    fixture = {**demo(), "owner_username": "synthetic-owner", "owner_password": "synthetic-fixture-password"}
    demo_file = tmp_path / "demo.json"
    demo_file.write_text(json.dumps(fixture))
    project_id, target_id, pool_id = (str(uuid4()) for _ in range(3))
    settings_path = f"/projects/{project_id}/storage/"
    target_path = settings_path + "targets/new/"
    toggle_path = settings_path + f"pools/{pool_id}/probes/"
    calls = []

    class FixtureClient:
        def __init__(self, _base_url, token=None):
            self.last_url = ""

        def login(self, _username, _password):
            pass

        def request(self, method, path, payload=None, **_kwargs):
            calls.append((method, path, payload))
            self.last_url = path
            if path == f"/clusters/{fixture['connection_id']}/":
                return "<p>Discovery: ready</p><p>slurm · brokerjob@slurm-fixture</p>".encode()
            if path == "/projects/new/":
                self.last_url = f"/projects/{project_id}/"
                return b""
            if path == f"/projects/{project_id}/keys/new/":
                return b"synthetic creation receipt"
            if path == "/api/v1/capabilities":
                return {"enabled_extensions": ["storage"]}
            if path == target_path + "?connection=" + fixture["connection_id"]:
                return '<select name="root_alias"><option value="staging-root-0">staging-root-0 — /home/brokerjob/jobs</option></select>'.encode()
            if path == target_path:
                self.last_url = settings_path
                return b""
            if path == settings_path + "?connection=" + fixture["connection_id"]:
                return f'<select name="results"><option value="{target_id}">Approved result location</option></select>'.encode()
            if path in {settings_path, toggle_path}:
                self.last_url = settings_path
                return b""
            if path == f"/api/v1/projects/{project_id}/storage":
                return {"project_id": project_id if inventory_matches else str(uuid4()),
                    "targets": [{"id": target_id, "connection_id": fixture["connection_id"], "pool_id": pool_id}],
                    "pools": [{"id": pool_id, "probe_enabled": True, "probe_supported": True, "observation": {
                        "source": "statvfs", "status": "available", "observed_at": datetime.now(timezone.utc).isoformat(),
                        "identity_verified": False, "total_bytes": 1000, "available_bytes": 500,
                        "total_inodes": 100, "available_inodes": 50, "quota": {"status": "unknown"}}}]}
            if path == f"/api/v1/projects/{project_id}/storage/plan":
                assert payload["estimates"][0]["target_id"] == target_id
                return {"dry_run": True, "advisory_only": True, "creates_reservations": False,
                        "enforced_at_dispatch": False, "status": "ready"}
            if path == f"/api/v1/projects/{project_id}/allowance":
                return {"jobs": {"admitted": 0}, "spent": {"cpu_core_seconds": 0, "gpu_device_seconds": 0},
                        "reserved": {"cpu_core_seconds": 0, "gpu_device_seconds": 0}}
            raise AssertionError("Unexpected disposable client route: " + path)

    monkeypatch.setattr(storage_probe_smoke, "Client", FixtureClient)
    monkeypatch.setattr(storage_probe_smoke, "revealed_key", lambda _human, _html: ("synthetic-project-token", str(uuid4())))
    if inventory_matches:
        storage_probe_smoke.run("http://fixture.invalid", demo_file, 1)
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["ok"] is True and receipt["project_id"] == project_id
        assert [payload for method, path, payload in calls if path == toggle_path] == [
            {"enabled": "false"}, {"enabled": "true"}, {"enabled": "false"}]
    else:
        with pytest.raises(SmokeFailure, match="project target selection"):
            storage_probe_smoke.run("http://fixture.invalid", demo_file, 1)
        assert not any(path == toggle_path for _method, path, _payload in calls)
    assert any(method == "POST" and path == target_path for method, path, _payload in calls)
    assert not any(path.startswith("/storage/") for _method, path, _payload in calls)
