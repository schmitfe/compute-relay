"""Advisory storage estimates and metadata-only probe boundaries."""

import copy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from compute_relay.extensions.storage.planning import (
    StoragePlanningError, assess_health, default_health_policy, plan_storage,
    validate_observation, validate_plan, validate_storage_roots,
)
from compute_relay.extensions.storage.probe import probe_filesystem, snapshot_from_statvfs

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


def observation(*, now=NOW, available_bytes=8000, available_inodes=8000, quota=None):
    return {"schema_version": "1.0", "observed_at": now.isoformat(), "source": "site_probe", "status": "available",
            "filesystem_id": "volume:test", "identity_verified": True, "total_bytes": 10000,
            "available_bytes": available_bytes, "total_inodes": 10000, "available_inodes": available_inodes,
            "quota": quota or {"status": "unknown"}}


def small_policy():
    return {**default_health_policy(), **{f"{severity}_floor_{unit}": 0 for severity in ("warning", "stop", "emergency", "recovery")
                                         for unit in ("bytes", "inodes")}}


def simple_inventory(snapshot=None):
    project_id, connection_id, target_id, pool_id = [str(uuid4()) for _ in range(4)]
    return {"project_id": project_id,
            "budget": {"bytes_budget": 20000, "inodes_budget": 20000, "retained_bytes_estimate": 100,
                       "retained_inodes_estimate": 10, "reserved_bytes_estimate": 200, "reserved_inodes_estimate": 20,
                       "estimate_updated_at": NOW.isoformat()},
            "bindings": [{"connection_id": connection_id, "targets": {"work": target_id, "results": target_id}}],
            "targets": [{"id": target_id, "connection_id": connection_id, "roles": ["work", "results"], "pool_id": pool_id}],
            "pools": [{"id": pool_id, "observation": snapshot or observation(), "health_policy": small_policy(),
                       "health_state": "normal", "recovery_since": None, "reserved_bytes_estimate": 200,
                       "reserved_inodes_estimate": 20}]}


def request(inventory, *, peak_bytes=1000, peak_inodes=100):
    return {"schema_version": "1.0", "connection_id": inventory["bindings"][0]["connection_id"],
            "estimates": [{"role": "work", "target_id": inventory["targets"][0]["id"],
                           "peak_bytes": peak_bytes, "peak_inodes": peak_inodes}]}


@pytest.mark.parametrize("available,expected", [(1100, "normal"), (900, "pressure"), (400, "critical"), (100, "emergency")])
@pytest.mark.parametrize("unit", ["bytes", "inodes"])
def test_capacity_and_inode_health_are_independent(available, expected, unit):
    data = observation(**{f"available_{unit}": available})
    assert assess_health(data, small_policy(), now=NOW)["state"] == expected


def test_stale_missing_identity_and_failed_observations_are_not_healthy():
    assert assess_health({}, now=NOW)["state"] == "unknown"
    assert assess_health(observation(now=NOW - timedelta(seconds=121)), now=NOW)["state"] == "stale"
    data = observation()
    data["identity_verified"] = False
    assert assess_health(data, now=NOW)["state"] == "unknown"
    data["status"] = "unavailable"
    assert assess_health(data, now=NOW)["state"] == "unavailable"


def test_critical_recovery_requires_fresh_continuous_headroom():
    first = assess_health(observation(), small_policy(), now=NOW, previous_state="critical")
    assert first["state"] == "recovering"
    later = NOW + timedelta(seconds=299)
    assert assess_health(observation(now=later), small_policy(), now=later, previous_state="recovering",
                         recovery_since=first["recovery_since"])["state"] == "recovering"
    after = NOW + timedelta(seconds=301)
    assert assess_health(observation(now=after), small_policy(), now=after, previous_state="recovering",
                         recovery_since=first["recovery_since"])["state"] == "normal"
    interrupted = assess_health(observation(available_bytes=600), small_policy(), now=NOW,
                                previous_state="recovering", recovery_since=first["recovery_since"])
    assert interrupted["state"] == "critical" and interrupted["recovery_since"] is None


def test_recovery_clock_restarts_after_a_probe_gap_and_unknown_inodes_cannot_clear_pressure():
    later = NOW + timedelta(minutes=6)
    result = assess_health(observation(now=later), small_policy(), now=later, previous_state="recovering",
        recovery_since=NOW.isoformat(), previous_observed_at=NOW.isoformat())
    assert result["state"] == "recovering" and result["recovery_since"] == later.isoformat()
    data = observation()
    data["total_inodes"] = data["available_inodes"] = None
    assert assess_health(data, small_policy(), now=NOW, previous_state="critical")["state"] == "critical"


@pytest.mark.parametrize("field,value", [("available_bytes", -1), ("total_inodes", True), ("available_inodes", 1.5),
                                        ("total_bytes", 2**53), ("available_bytes", 10001)])
def test_observations_reject_invalid_or_unsafe_counters(field, value):
    data = observation()
    data[field] = value
    with pytest.raises(StoragePlanningError):
        validate_observation(data, now=NOW)


def test_future_timestamps_and_secret_exception_fields_are_rejected():
    with pytest.raises(StoragePlanningError):
        validate_observation(observation(now=NOW + timedelta(minutes=1)), now=NOW)
    data = observation()
    data["traceback"] = "private identity file must not be persisted"
    with pytest.raises(StoragePlanningError):
        validate_observation(data, now=NOW)
    data = observation()
    data["error_code"] = "/private/key: denied"
    with pytest.raises(StoragePlanningError):
        validate_observation(data, now=NOW)


def test_planning_is_pure_and_does_not_double_subtract_retained_usage():
    fixture = simple_inventory(observation(quota={"status": "reported", "available_bytes": 9000, "available_inodes": 9000}))
    before = copy.deepcopy(fixture)
    result = plan_storage(request(fixture), fixture, now=NOW)
    assert result["status"] == "ready"
    assert result["estimates"]["projected_bytes_estimate"] == 1300
    assert result["pools"][0]["headroom_bytes_estimate"] == 8000 - 200 - 1000 - 500
    assert result["dry_run"] and result["advisory_only"] and not result["creates_reservations"] and not result["enforced_at_dispatch"]
    assert fixture == before


def test_native_quota_and_project_budgets_are_separate_from_free_filesystem():
    fixture = simple_inventory(observation(quota={"status": "reported", "available_bytes": 900, "available_inodes": 9000}))
    result = plan_storage(request(fixture), fixture, now=NOW)
    assert result["status"] == "blocked"
    assert any("reported_quota_bytes_insufficient" in reason for reason in result["reasons"])
    fixture["budget"]["inodes_budget"] = 50
    assert "project_inodes_budget_exceeded" in plan_storage(request(fixture), fixture, now=NOW)["reasons"]


def test_unknown_quota_and_unsupported_inode_counts_remain_explicit():
    data = observation()
    data["total_inodes"] = data["available_inodes"] = 0
    fixture = simple_inventory(data)
    result = plan_storage(request(fixture), fixture, now=NOW)
    assert result["status"] == "unknown"
    assert any("filesystem_inodes_unknown" in item for item in result["unknowns"])
    assert not any("inodes_headroom_insufficient" in item for item in result["reasons"])
    assert any("native_quota_unknown" in item for item in result["unknowns"])


def test_multiple_roles_share_one_pool_and_sum_growth_conservatively():
    fixture = simple_inventory()
    document = request(fixture)
    document["estimates"].append({**document["estimates"][0], "role": "results", "peak_bytes": 2000})
    result = plan_storage(document, fixture, now=NOW)
    assert len(result["pools"]) == 1 and result["pools"][0]["peak_bytes"] == 3000


def test_aggregate_headroom_cannot_emit_integers_outside_wire_range():
    fixture = simple_inventory()
    fixture["budget"].update(bytes_budget=None, retained_bytes_estimate=0, reserved_bytes_estimate=0)
    fixture["pools"][0]["reserved_bytes_estimate"] = 2**53 - 1
    with pytest.raises(StoragePlanningError, match="safe integers"):
        plan_storage(request(fixture, peak_bytes=1000), fixture, now=NOW)


@pytest.mark.parametrize("change", [
    {"path": "/arbitrary/target"}, {"target_id": "/arbitrary/target"}, {"peak_bytes": True},
    {"peak_inodes": 0.5}, {"peak_bytes": 2**53}, {"retained_bytes": 1001}, {"role": "credentials"},
])
def test_agent_plan_cannot_supply_paths_or_unsafe_estimates(change):
    fixture = simple_inventory()
    document = request(fixture)
    document["estimates"][0].update(change)
    with pytest.raises(StoragePlanningError):
        validate_plan(document)


def test_plan_denies_wrong_cluster_target_and_duplicate_role():
    fixture = simple_inventory()
    document = request(fixture)
    document["estimates"][0]["target_id"] = str(uuid4())
    with pytest.raises(StoragePlanningError, match="selected target"):
        plan_storage(document, fixture, now=NOW)
    document = request(fixture)
    document["estimates"] *= 2
    with pytest.raises(StoragePlanningError, match="distinct"):
        validate_plan(document)


@pytest.mark.parametrize("path", ["/", "/safe/../private", "//safe/root", "https://storage.invalid/data", "/safe/root;command"])
def test_protected_storage_root_metadata_rejects_paths_and_endpoints(path):
    with pytest.raises(StoragePlanningError):
        validate_storage_roots([{"alias": "root", "path": path, "roles": ["work"]}])


def test_statvfs_conversion_uses_user_available_blocks_and_marks_unknown_identity():
    data = snapshot_from_statvfs({"f_frsize": 4096, "f_blocks": 100, "f_bavail": 10, "f_files": 0, "f_favail": 0},
                                filesystem_id="1:2", observed_at=NOW)
    assert data["available_bytes"] == 40960
    assert data["total_inodes"] is None and data["available_inodes"] is None
    assert data["identity_verified"] is False
    mismatch = snapshot_from_statvfs({"f_frsize": 4096, "f_blocks": 100, "f_bavail": 10, "f_files": 100, "f_favail": 10},
        filesystem_id="1:2", expected_filesystem_id="9:9", observed_at=NOW)
    assert mismatch["status"] == "unavailable"
    unknown_inodes = snapshot_from_statvfs({"f_frsize": 4096, "f_blocks": 100, "f_bavail": 10,
        "f_files": 2**64 - 1, "f_favail": 2**64 - 1}, filesystem_id="1:2", observed_at=NOW)
    assert unknown_inodes["available_inodes"] is None and unknown_inodes["available_bytes"] == 40960


def test_unused_probe_reads_only_directory_metadata_and_never_follows_links(tmp_path, monkeypatch):
    from compute_relay.extensions.storage import probe
    root = tmp_path / "enrolled"
    root.mkdir()
    monkeypatch.setattr(probe.os, "scandir", lambda *_args: pytest.fail("probe scanned directories"))
    data = probe_filesystem(str(root), observed_at=NOW)
    assert data["source"] == "statvfs" and data["identity_verified"] is False
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        probe_filesystem(str(alias), observed_at=NOW)
