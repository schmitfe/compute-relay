"""Pure saved-fact derivation and human edits; no scheduler/SSH/DB mutations."""
from test_support.unit_test_cluster_configuration import form_data, snapshot

from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys

import pytest

from compute_relay.control.cluster_configuration import ClusterConfigurationForm
from compute_relay.domain.cluster_configuration import (
    ClusterConfigurationError, derive_cluster_configuration, validate_cluster_configuration,
)
from test_support.unit_test_backends import sample, REVISION






def test_maxima_are_single_node_candidates_not_partition_or_cluster_totals(snapshot):
    original = deepcopy(snapshot)
    result = derive_cluster_configuration(snapshot)
    assert result["discovered_limits"] == {"maximum_cpus": 128, "maximum_gpus": 8,
        "maximum_memory_mib": 512000, "maximum_time_limit_seconds": 172800}
    assert result["partitions"][0]["maximum_cpus"] == 32
    assert result["partitions"][0]["maximum_memory_mib"] == 128000
    assert result["partitions"][0]["maximum_gpus"] == 0
    assert result["site_profile"]["automatic_execution_allowed"] is False
    assert not any(key in result["site_profile"] for key in ["cleanup_margin_seconds", "termination_margin_seconds", "allocated_cpus"])
    assert snapshot == original


def test_gpu_maximum_matches_one_selectable_type_not_sum_of_types(snapshot):
    snapshot["nodes"] = [snapshot["nodes"][1]]
    result = derive_cluster_configuration(snapshot)
    assert result["discovered_limits"]["maximum_gpus"] == 4
    gpu = next(item for item in result["partitions"] if item["name"] == "gpu")
    assert gpu["gpu_types"] == {"a100": 4, "rtx": 2}


@pytest.mark.parametrize("gres, count", [("gpu:4,gpu:a100:4", 4), ("gpu:8", 8),
    ("gpu:a100:4,gpu:rtx:2", 4), ("mps:100,shard:16", 0), ("gpu:a100:4,gpu:a100:4", None),
    ("gpu:6,gpu:a100:4", None), ("gpu:unknown", None), ("gpu:a100:1K", None)])
def test_gpu_count_unknowns_and_double_counting(gres, count):
    result = derive_cluster_configuration({"nodes": [{"partition": "gpu", "cpus": 16, "memory_mib": 1024, "gres": gres}]})
    assert result["discovered_limits"]["maximum_gpus"] == count
    if count is None:
        assert "maximum_gpus" not in result["site_profile"]


@pytest.mark.parametrize("value, expected", [("00:01:59", 60), ("1-02:03:04", 93780),
    ("60", 3600), ("90:30", 5400), ("1-02", 93600), ("UNLIMITED", 604800),
    ("INFINITE", 604800), ("9-00:00:00", 604800), ("N/A", None), ("00:00:59", None),
    ("00:99:00", None), ("-1", None), (True, None)])
def test_walltime_is_finite_and_floor_rounded_to_scheduler_minutes(value, expected):
    result = derive_cluster_configuration({"partitions": [{"PartitionName": "cpu", "MaxTime": value}]})
    assert result["discovered_limits"]["maximum_time_limit_seconds"] == expected


def test_implementation_caps_are_visible_in_evidence():
    result = derive_cluster_configuration({"nodes": [{"partition": "large", "cpus": "4096",
        "memory_mib": "99999999", "gres": "gpu:a100:256"}],
        "partitions": [{"PartitionName": "large", "MaxTime": "UNLIMITED"}]})
    assert result["discovered_limits"] == {"maximum_cpus": 1024, "maximum_gpus": 16,
        "maximum_memory_mib": 16777216, "maximum_time_limit_seconds": 604800}
    assert any("implementation limit" in warning for warning in result["warnings"])
    assert any("Unlimited scheduler" in warning for warning in result["warnings"])


def test_per_cpu_memory_applies_to_observed_single_node_cpu_ceiling(snapshot):
    snapshot["partitions"][0].update(MaxMemPerNode="UNLIMITED", MaxMemPerCPU="1024")
    result = derive_cluster_configuration(snapshot)
    assert result["partitions"][0]["maximum_memory_mib"] == 32768


def test_unparseable_partition_caps_are_unknown_instead_of_ignored(snapshot):
    snapshot["partitions"][0].update(MaxCPUsPerNode="unreported", MaxMemPerNode="unreported")
    result = derive_cluster_configuration(snapshot)
    assert result["partitions"][0]["maximum_cpus"] is None
    assert result["partitions"][0]["maximum_memory_mib"] is None
    assert any("unsupported or malformed" in warning for warning in result["warnings"])


def test_accounts_and_qos_come_only_from_successful_own_association_snapshot(snapshot):
    result = derive_cluster_configuration(snapshot)
    assert result["site_profile"]["accounts"] == ["science"]
    assert result["site_profile"]["qos"] == ["high", "normal"]
    assert "partition-qos" not in result["site_profile"]["qos"]
    snapshot["association_query_status"] = "unknown"
    result = derive_cluster_configuration(snapshot)
    assert result["site_profile"]["accounts"] == result["site_profile"]["qos"] == []
    assert {"accounts", "qos"} <= set(result["unknowns"])


def test_missing_and_malformed_discovery_remains_unknown_without_minimum_guesses():
    result = derive_cluster_configuration({"nodes": [None, {"partition": "cpu", "cpus": True, "memory_mib": "unknown", "gres": None}],
        "partitions": [None, {"PartitionName": "bad;id"}, "debug"], "associations": "not-a-list"})
    assert result["site_profile"] == {"partitions": ["cpu", "debug"], "accounts": [], "qos": [], "automatic_execution_allowed": False}
    assert all(result["discovered_limits"][field] is None for field in result["discovered_limits"])
    assert result["unknown_reasons"]["maximum_time_limit_seconds"]
    assert any("missing" in warning for warning in result["warnings"])


def test_owner_limits_empty_allowlists_and_existing_verified_safety_are_not_overwritten(snapshot):
    current = {"maximum_cpus": 16, "accounts": [], "qos": [], "allocated_cpus": 8,
        "allocated_gpus": 0, "automatic_execution_allowed": True}
    original = deepcopy(current)
    result = derive_cluster_configuration(snapshot, current)
    assert all(result["site_profile"][key] == value for key, value in current.items())
    assert not any("margin_seconds" in key for key in result["site_profile"])
    assert current == original
    result["site_profile"]["accounts"].append("different")
    assert current == original


def test_new_suggestion_cannot_contradict_retained_allocation_floor():
    with pytest.raises(ClusterConfigurationError, match="rounding"):
        derive_cluster_configuration({"nodes": [{"partition": "small", "cpus": "4", "memory_mib": "1024", "gres": "(null)"}]},
            {"allocated_cpus": 8})


def test_resource_discovery_prepares_without_timing_assumptions_but_preserves_review_gate(sample):
    from compute_relay.backends import prepare
    request, environment, connection, _ = sample
    connection["backend"] = "slurm"
    connection["site_profile"] = derive_cluster_configuration({"nodes": [{"partition": "debug", "cpus": "4",
        "memory_mib": "8192", "gres": "(null)"}], "partitions": [{"PartitionName": "debug", "MaxTime": "01:00:00"}],
        "association_query_status": "available", "associations": [{"account": "broker"}]})["site_profile"]
    manifest = prepare(request, environment, connection, REVISION)["manifest"]
    assert manifest["maximum_allocation"]["time_limit_seconds"] == request["resources"]["time_limit_seconds"]
    assert connection["site_profile"]["automatic_execution_allowed"] is False


def test_verified_allocation_rounding_uses_scheduler_walltime_for_prepared_reservation(sample):
    from compute_relay.backends import prepare
    request, environment, connection, _ = sample
    connection["backend"] = "slurm"
    current = {"partitions": ["debug"], "accounts": ["broker"], "allocated_cpus": 4}
    connection["site_profile"] = derive_cluster_configuration({"nodes": [{"partition": "debug", "cpus": "32",
        "memory_mib": "8192", "gres": "(null)"}]}, current)["site_profile"]
    manifest = prepare(request, environment, connection, REVISION)["manifest"]
    assert manifest["maximum_allocation"]["cpu_cores"] == 4
    assert manifest["reservation"] == {"cpu_core_seconds": 240, "gpu_device_seconds": 0}


def test_domain_import_and_derivation_work_with_controller_and_django_imports_blocked():
    source = Path(__file__).resolve().parents[2] / "src"
    program = """import builtins
original = builtins.__import__
def checked(name, *args, **kwargs):
    if name == 'django' or name.startswith('django.') or name.startswith('compute_relay.control'):
        raise AssertionError('controller dependency in worker configuration')
    return original(name, *args, **kwargs)
builtins.__import__ = checked
from compute_relay.domain.cluster_configuration import derive_cluster_configuration
assert derive_cluster_configuration({})['site_profile']['automatic_execution_allowed'] is False
"""
    result = subprocess.run([sys.executable, "-c", program], env={"PYTHONPATH": str(source), "PATH": os.defpath},
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_form_uses_maximal_missing_defaults_and_preserves_auto_review(snapshot):
    form = ClusterConfigurationForm(snapshot=snapshot, current_profile={"automatic_execution_allowed": False})
    assert form.initial["maximum_cpus"] == 128
    assert form.initial["maximum_gpus"] == 8
    assert form.initial["partitions"] == "cpu, gpu"
    form = ClusterConfigurationForm(form_data(automatic_execution_allowed=True, allocated_cpus=1),
        snapshot=snapshot, current_profile={"automatic_execution_allowed": False})
    assert form.is_valid(), form.errors
    profile = form.site_profile()
    assert profile["automatic_execution_allowed"] is False and "cleanup_margin_seconds" not in profile
    assert "allocated_cpus" not in profile


def test_form_keeps_checked_automatic_setting_and_does_not_edit_verified_floor(snapshot):
    form = ClusterConfigurationForm(form_data(allow_automatic_execution=True), snapshot=snapshot,
        current_profile={"automatic_execution_allowed": True, "allocated_cpus": 8})
    assert form.is_valid(), form.errors
    assert form.site_profile()["automatic_execution_allowed"] is True
    lower = ClusterConfigurationForm(form_data(maximum_cpus=4), snapshot=snapshot, current_profile={"allocated_cpus": 8})
    assert not lower.is_valid()


def test_cluster_automatic_execution_is_explicit_opt_in_and_can_be_unchecked(snapshot):
    initial = ClusterConfigurationForm(snapshot=snapshot)
    assert initial.initial["allow_automatic_execution"] is False
    enabled = ClusterConfigurationForm(form_data(allow_automatic_execution=True), snapshot=snapshot,
        current_profile={"automatic_execution_allowed": False})
    assert enabled.is_valid(), enabled.errors
    assert enabled.site_profile()["automatic_execution_allowed"] is True
    existing = ClusterConfigurationForm(snapshot=snapshot, current_profile=enabled.site_profile())
    assert existing.initial["allow_automatic_execution"] is True
    disabled = ClusterConfigurationForm(form_data(), snapshot=snapshot, current_profile=enabled.site_profile())
    assert disabled.is_valid(), disabled.errors
    assert disabled.site_profile()["automatic_execution_allowed"] is False


@pytest.mark.parametrize("field, value", [("maximum_cpus", 0), ("maximum_cpus", 1025),
    ("maximum_gpus", -1), ("maximum_gpus", 17), ("maximum_memory_mib", 127),
    ("maximum_memory_mib", 16777217), ("maximum_time_limit_seconds", 61),
    ("maximum_time_limit_seconds", 604860), ("maximum_cpus", "")])
def test_final_owner_edits_require_supported_finite_limits(field, value):
    form = ClusterConfigurationForm(form_data(**{field: value}), snapshot={})
    assert not form.is_valid()


@pytest.mark.parametrize("field, value", [("maximum_cpus", 129), ("maximum_gpus", 9),
    ("maximum_memory_mib", 512001), ("maximum_time_limit_seconds", 172860)])
def test_owner_edits_cannot_exceed_known_selected_partition_ceiling(snapshot, field, value):
    form = ClusterConfigurationForm(form_data(**{field: value}), snapshot=snapshot)
    assert not form.is_valid() and field in form.errors


def test_selecting_only_small_partition_restricts_known_ceilings(snapshot):
    form = ClusterConfigurationForm(form_data(partitions="cpu", maximum_cpus=64), snapshot=snapshot)
    assert not form.is_valid() and "maximum_cpus" in form.errors


@pytest.mark.parametrize("value", ["cpu;id", "cpu\nother", "-option", "cpu, cpu", ",,"])
def test_scheduler_name_injection_duplicates_and_empty_partition_lists_are_denied(value):
    form = ClusterConfigurationForm(form_data(partitions=value), snapshot={})
    assert not form.is_valid()


def test_unknown_facts_require_explicit_owner_values_and_allow_manual_safe_names():
    missing = ClusterConfigurationForm({"partitions": "private"}, snapshot={})
    assert not missing.is_valid()
    explicit = ClusterConfigurationForm(form_data(partitions="private", accounts="manual-account", qos=""), snapshot={})
    assert explicit.is_valid(), explicit.errors
    result = explicit.site_profile()
    assert result["partitions"] == ["private"] and result["automatic_execution_allowed"] is False
    assert "cleanup_margin_seconds" not in result and "allocated_cpus" not in result




def test_cluster_configuration_accepts_resources_without_separate_timing_fields(snapshot):
    form = ClusterConfigurationForm(form_data(), snapshot=snapshot)
    assert form.is_valid(), form.errors
    assert not any("margin_seconds" in field for field in form.site_profile())








@pytest.mark.parametrize("profile", [{"maximum_cpus": True}, {"maximum_memory_mib": 2**53},
    {"automatic_execution_allowed": "true"}, {"ssh": {"host": "injected"}}])
def test_pure_validation_rejects_noncanonical_or_unsupported_values(profile):
    with pytest.raises(ClusterConfigurationError):
        validate_cluster_configuration(profile, require_limits=False)


@pytest.mark.parametrize("field", ["termination_margin_seconds", "overtime_margin_seconds", "cleanup_margin_seconds", "unsupported_limit"])
def test_profile_rejects_unknown_settings_instead_of_silently_rewriting_them(field):
    with pytest.raises(ClusterConfigurationError, match="unsupported"):
        validate_cluster_configuration({field: 30}, require_limits=False)
