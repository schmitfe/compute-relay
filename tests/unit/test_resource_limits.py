"""Configured planning ceilings match allocation rounding and requested walltime."""

from copy import deepcopy

import pytest

from compute_relay.domain.resource_limits import allocation_violations, effective_resource_limits


@pytest.fixture
def policy():
    return {"mode": "budgeted", "per_job_limits": {"cpu_cores": 16, "gpu_devices": 2,
        "memory_mib": 8192, "time_limit_seconds": 600}}


@pytest.fixture
def configuration():
    return {"site_profile": {"maximum_cpus": 8, "maximum_gpus": 4,
        "maximum_memory_mib": 16384, "maximum_time_limit_seconds": 1200,
        "allocated_cpus": 2, "allocated_gpus": 0}}


def test_project_cluster_and_schema_intersection_preserves_inputs(policy, configuration):
    original = deepcopy((policy, configuration))
    result = effective_resource_limits(policy, configuration)
    assert result["per_job_limits"] == {"nodes": 1, "cpu_cores": 8, "gpu_devices": 2,
        "memory_mib": 8192, "time_limit_seconds": 600}
    assert result["allocation_floors"] == {"cpu_cores": 2, "gpu_devices": 0}
    assert result["minimum_request"] == {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60}
    assert result["feasible"] is True
    assert result["unknowns"] == []
    assert (policy, configuration) == original
    result["project_limits"]["cpu_cores"] = 999
    result["cluster_limits"]["maximum_cpus"] = 999
    assert (policy, configuration) == original


@pytest.mark.parametrize("walltime, expected_walltime", [(600, 600), (599, 540), (120, 120), (119, 60), (60, 60), (59, 0)])
def test_walltime_is_rounded_down_to_supported_minutes(policy, configuration, walltime, expected_walltime):
    policy["per_job_limits"]["time_limit_seconds"] = walltime
    result = effective_resource_limits(policy, configuration)
    assert result["per_job_limits"]["time_limit_seconds"] == expected_walltime
    assert result["feasible"] is bool(expected_walltime)


def test_smaller_cluster_walltime_is_used(policy, configuration):
    configuration["site_profile"]["maximum_time_limit_seconds"] = 125
    result = effective_resource_limits(policy, configuration)
    assert result["per_job_limits"]["time_limit_seconds"] == 120


@pytest.mark.parametrize("resource,floor,cap", [("cpu_cores", "allocated_cpus", 1), ("gpu_devices", "allocated_gpus", 0)])
def test_allocation_floor_cannot_fit_smaller_project_envelope(policy, configuration, resource, floor, cap):
    policy["per_job_limits"][resource] = cap
    configuration["site_profile"][floor] = cap + 1
    result = effective_resource_limits(policy, configuration)
    assert result["feasible"] is False
    assert resource in result["blocking_reasons"][0]
    assert str(cap + 1) in result["blocking_reasons"][0]


def test_zero_gpu_ceiling_is_valid(policy, configuration):
    policy["per_job_limits"]["gpu_devices"] = 0
    configuration["site_profile"]["maximum_gpus"] = 0
    result = effective_resource_limits(policy, configuration)
    assert result["per_job_limits"]["gpu_devices"] == 0
    assert result["feasible"] is True


def test_planning_reports_the_supported_resource_dimensions(policy, configuration):
    result = effective_resource_limits(policy, configuration)
    assert result["per_job_limits"]["time_limit_seconds"] == 600
    assert result["feasible"] is True
    assert not any("margin" in key or "charge_duration" in key for key in result)
    assert "maximum_charge_duration_seconds" not in result["per_job_limits"]
    assert "maximum_charge_duration_seconds" not in result["project_limits"]


@pytest.mark.parametrize("backend", ["fake", "slurm"])
def test_no_timing_configuration_is_needed_for_any_backend(policy, backend):
    result = effective_resource_limits(policy, {}, backend=backend)
    assert result["per_job_limits"]["time_limit_seconds"] == 600
    assert result["allocation_floors"] == {"cpu_cores": 1, "gpu_devices": 0}
    assert result["feasible"] is True
    assert result["cluster_limits"]["maximum_cpus"] is None
    assert "cluster_limits.maximum_cpus" in result["unknowns"]


@pytest.mark.parametrize("bad", [True, False, None, "16", 16.0, float("inf"), -1, 2**54])
@pytest.mark.parametrize("location", ["project", "cluster", "floor"])
def test_malformed_configured_values_never_broaden_an_effective_limit(policy, configuration, location, bad):
    if location == "project":
        policy["per_job_limits"]["cpu_cores"] = bad
    else:
        field = {"cluster": "maximum_cpus", "floor": "allocated_cpus"}[location]
        configuration["site_profile"][field] = bad
    result = effective_resource_limits(policy, configuration)
    assert result["feasible"] is None
    assert result["unknowns"]
    if location in {"project", "cluster"}:
        assert result["per_job_limits"]["cpu_cores"] is None
    else:
        assert result["allocation_floors"]["cpu_cores"] is None


def test_missing_required_project_cap_does_not_use_cluster_as_substitute(policy, configuration):
    del policy["per_job_limits"]["time_limit_seconds"]
    result = effective_resource_limits(policy, configuration)
    assert result["per_job_limits"]["time_limit_seconds"] is None
    assert result["feasible"] is None


def test_version_one_caps_are_enforced_even_when_both_sources_are_larger(policy, configuration):
    policy["per_job_limits"] = {"cpu_cores": 10000, "gpu_devices": 10000, "memory_mib": 10**9,
        "time_limit_seconds": 10**9}
    for field in ("maximum_cpus", "maximum_gpus", "maximum_memory_mib", "maximum_time_limit_seconds"):
        configuration["site_profile"][field] = 10**9
    result = effective_resource_limits(policy, configuration)
    assert result["per_job_limits"] == {"nodes": 1, "cpu_cores": 1024, "gpu_devices": 16,
        "memory_mib": 16777216, "time_limit_seconds": 604800}


@pytest.mark.parametrize("mode,override", [("budgeted", False), ("approval_required", False), ("mixed", True)])
def test_mixed_mode_keeps_limits_and_explicitly_describes_human_override(policy, configuration, mode, override):
    normal = effective_resource_limits(policy, configuration)
    policy["mode"] = mode
    result = effective_resource_limits(policy, configuration)
    assert result["per_job_limits"] == normal["per_job_limits"]
    assert result["project_envelope_human_override_allowed"] is override


def test_impossible_known_ceiling_wins_over_other_unknowns(policy, configuration):
    policy["per_job_limits"]["memory_mib"] = 100
    del policy["per_job_limits"]["time_limit_seconds"]
    result = effective_resource_limits(policy, configuration)
    assert result["feasible"] is False
    assert "memory_mib" in result["blocking_reasons"][0]


def test_all_violations_report_requested_effective_and_project_limits(policy):
    resources = {"cpus": 1, "gpus": 0, "memory_mib": 16384, "time_limit_seconds": 601}
    allocation = {"cpu_cores": 32, "gpu_devices": 4, "time_limit_seconds": 601}
    violations = allocation_violations(resources, allocation, policy["per_job_limits"])
    assert [entry["resource"] for entry in violations] == ["cpu_cores", "gpu_devices", "memory_mib", "time_limit_seconds"]
    assert violations[0] == {"resource": "cpu_cores", "requested": 1, "effective": 32, "limit": 16,
        "excess": 16, "source": "project", "unit": "cores"}
    assert violations[1]["requested"] == 0
    assert violations[2]["effective"] == 16384
    assert violations[2]["unit"] == "MiB"
    assert violations[-1]["requested"] == 601
    assert violations[-1]["effective"] == 601


def test_exact_boundaries_are_allowed_including_zero_gpus(policy):
    limits = policy["per_job_limits"]
    limits["gpu_devices"] = 0
    allocation = {key: value for key, value in limits.items() if key != "memory_mib"}
    resources = {"cpus": limits["cpu_cores"], "gpus": 0, "memory_mib": limits["memory_mib"], "time_limit_seconds": 600}
    assert allocation_violations(resources, allocation, limits) == []


@pytest.mark.parametrize("bad", [None, True, -1, float("inf"), "16"])
def test_malformed_allocation_cannot_turn_into_no_violations(policy, bad):
    allocation = {**policy["per_job_limits"], "cpu_cores": bad}
    with pytest.raises(ValueError, match="cpu_cores"):
        allocation_violations({"memory_mib": 128}, allocation, policy["per_job_limits"])


def test_minimal_existing_internal_request_keeps_unknown_requested_count(policy):
    allocation = {**policy["per_job_limits"], "cpu_cores": 32}
    result = allocation_violations({"memory_mib": 128}, allocation, policy["per_job_limits"])
    assert len(result) == 1
    assert result[0]["requested"] is None
