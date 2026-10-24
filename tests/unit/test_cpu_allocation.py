"""CPU reservation predictions use logical allocation units, without extra time."""

from copy import deepcopy

import pytest

from compute_relay.backends import BackendError, prepare
from compute_relay.backends.discovery import parse_discovery
from compute_relay.control.resource_api import planned_allocation
from compute_relay.domain.allocation import allocated_cpus, cpu_allocation_candidate
from compute_relay.domain.cluster_configuration import validate_cluster_configuration, ClusterConfigurationError
from compute_relay.domain.resource_limits import effective_resource_limits
from test_backends import sample, REVISION  # noqa: F401
from test_control import broker, prepared_job  # noqa: F401
from test_connector_setup import personal, configuration_fields, lease, reply  # noqa: F401


def topology(*threads, parameters="CR_CORE_MEMORY"):
    return {"scheduler_settings": {"SelectType": "select/cons_tres", "SelectTypeParameters": parameters},
        "partitions": [{"PartitionName": "cpu", "SelectTypeParameters": "NONE", "OverSubscribe": "NO"}],
        "nodes": [{"name": f"n{index}", "partition": "cpu", "cpus": str(4 * count), "sockets": "2",
            "cores_per_socket": "2", "threads_per_core": str(count)} for index, count in enumerate(threads)]}


@pytest.mark.parametrize("requested,floor,unit,expected", [(1, 1, 2, 2), (2, 1, 2, 2), (3, 1, 2, 4),
    (4, 1, 2, 4), (5, 1, 2, 6), (1, 3, 2, 4), (7, 1, 4, 8)])
def test_minimum_floor_and_quantum_are_distinct(requested, floor, unit, expected):
    assert allocated_cpus(requested, {"allocated_cpus": floor, "cpu_allocation_quantum": unit}) == expected


@pytest.mark.parametrize("bad", [0, -1, True, 2.0, "2", None, 1025])
def test_bad_explicit_unit_is_never_silently_replaced(bad):
    with pytest.raises(ValueError):
        allocated_cpus(1, {"cpu_allocation_quantum": bad})
    with pytest.raises(ClusterConfigurationError):
        validate_cluster_configuration({"cpu_allocation_quantum": bad}, require_limits=False)


def test_topology_infers_whole_core_unit_advisory_without_mutating_inputs():
    snapshot = topology(2, 2)
    original = deepcopy(snapshot)
    result = cpu_allocation_candidate(snapshot)
    assert result["status"] == "known" and result["value"] == 2
    assert "Provisional" in result["note"]
    assert snapshot == original and "cpu_allocation_quantum" not in snapshot


def test_heterogeneous_node_grains_use_lcm_not_maximum():
    assert cpu_allocation_candidate(topology(2, 3))["value"] == 6


def test_partition_override_and_socket_units():
    snapshot = topology(2, parameters="CR_CPU_MEMORY")
    assert cpu_allocation_candidate(snapshot)["value"] == 1
    snapshot["partitions"][0]["SelectTypeParameters"] = "CR_SOCKET_MEMORY"
    assert cpu_allocation_candidate(snapshot)["value"] == 4


@pytest.mark.parametrize("change", ["missing_threads", "inconsistent", "no_nodes", "unsupported_select", "exclusive", "missing_partition", "null_nodes"])
def test_incomplete_or_unsupported_facts_remain_unknown(change):
    snapshot = topology(2)
    if change == "missing_threads":
        snapshot["nodes"][0].pop("threads_per_core")
    elif change == "inconsistent":
        snapshot["nodes"][0]["cpus"] = "6"
    elif change in {"no_nodes", "null_nodes"}:
        snapshot["nodes"] = [] if change == "no_nodes" else None
    elif change == "unsupported_select":
        snapshot["scheduler_settings"]["SelectType"] = "select/linear"
    elif change == "exclusive":
        snapshot["partitions"][0]["OverSubscribe"] = "EXCLUSIVE"
    else:
        snapshot["partitions"] = []
    result = cpu_allocation_candidate(snapshot, ["cpu"])
    assert result["status"] == "unknown" and result["value"] is None


def test_only_selected_partitions_contribute_to_candidate():
    snapshot = topology(2)
    snapshot["partitions"].append({"PartitionName": "unknown"})
    assert cpu_allocation_candidate(snapshot)["value"] is None
    assert cpu_allocation_candidate(snapshot, ["cpu"])["value"] == 2


def test_node_discovery_requires_the_current_topology_columns():
    result = parse_discovery({"nodes": {"stdout": "n0|cpu*|40|180000|(null)|idle|2|10|2\nn1|invalid|8|1024|(null)|down"}}, {})
    assert result["nodes"][0]["threads_per_core"] == "2"
    assert result["nodes"][0]["cores_per_socket"] == "10"
    assert result["nodes"][0]["partition"] == "cpu"
    assert len(result["nodes"]) == 1


def test_preparation_rounds_odd_counts_without_changing_requested_cpus_or_walltime(sample):
    request, environment, config, _ = sample
    request["resources"]["cpus"] = 3
    config["site_profile"].update(cpu_allocation_quantum=2, maximum_cpus=8)
    prepared = prepare(request, environment, config, REVISION)
    assert prepared["manifest"]["maximum_allocation"]["cpu_cores"] == 4
    assert prepared["manifest"]["reservation"]["cpu_core_seconds"] == 240
    assert "#SBATCH --cpus-per-task=3\n" in prepared["generated_files"]["_broker/batch.sh"]
    assert "#SBATCH --time=1\n" in prepared["generated_files"]["_broker/batch.sh"]
    assert planned_allocation(request["resources"], config)["cpu_cores"] == 4
    config["site_profile"]["maximum_cpus"] = 3
    with pytest.raises(BackendError, match="Effective CPU"):
        prepare(request, environment, config, REVISION)


def test_context_describes_request_limit_unit_and_unknown_prediction(sample):
    _, _, config, _ = sample
    policy = {"mode": "budgeted", "per_job_limits": {"cpu_cores": 3, "gpu_devices": 0,
        "memory_mib": 1024, "time_limit_seconds": 60}}
    config["site_profile"].update(cpu_allocation_quantum=2, maximum_cpus=8)
    result = effective_resource_limits(policy, config)
    assert result["per_job_limits"]["cpu_cores"] == 3
    assert result["maximum_request_cpus"] == 2
    assert result["allocation_quantum"] == {"cpu_cores": 2}
    assert result["allocation_prediction"]["basis"] == "configured_unit"
    policy["per_job_limits"]["cpu_cores"] = 1
    assert effective_resource_limits(policy, config)["feasible"] is False
    del config["site_profile"]["cpu_allocation_quantum"]
    result = effective_resource_limits(policy, config)
    assert result["allocation_prediction"]["basis"] == "request_and_configured_floor_only"
    assert result["allocation_prediction"]["scheduler_verified"] is False


def test_rounded_reservation_is_held_before_approval_and_cannot_overbook(broker):
    from compute_relay.control import services
    from compute_relay.control.errors import BrokerError
    broker.connection.configuration = {**broker.connection.configuration, "site_profile": {"cpu_allocation_quantum": 2}}
    broker.connection.save(update_fields=["configuration"])
    job = prepared_job(broker)
    assert job.manifest["maximum_allocation"]["cpu_cores"] == 2
    assert job.manifest["reservation"]["cpu_core_seconds"] == 120
    broker.grant.policy["allowance"]["cpu_core_seconds"] = 100
    broker.grant.save(update_fields=["policy"])
    with pytest.raises(BrokerError) as caught:
        services.request_execution(job, broker.credential)
    assert caught.value.code == "BUDGET_UNAVAILABLE"
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 0 and not job.execution_requests.exists()
    broker.grant.policy["allowance"]["cpu_core_seconds"] = 240
    broker.grant.save(update_fields=["policy"])
    services.request_execution(job, broker.credential)
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 120


@pytest.mark.django_db
def test_cpu_unit_requires_updated_worker_and_normal_configuration_acknowledgement(personal):
    from compute_relay.control.models import ConnectorTask
    from compute_relay.worker.enrollment import profile_metadata, resolve_connection
    context = personal
    path = f"/clusters/{context.connection.id}/configuration/"
    data = configuration_fields(context, cpu_allocation_quantum=2)
    denied = context.human.post(path, data)
    assert denied.status_code == 409 and b"Restart your personal connector" in denied.content
    assert not ConnectorTask.objects.exists()
    context.connection.refresh_from_db()
    assert context.connection.setup_complete
    context.worker.connector_features.append("personal_cpu_allocation_v1")
    context.worker.save(update_fields=["connector_features"])
    assert context.human.post(path, data).status_code == 302
    context.connection.refresh_from_db()
    assert "cpu_allocation_quantum" not in context.connection.configuration["site_profile"]
    assert not context.connection.setup_complete
    task = lease(context)
    protected = {**context.protected, "remote_root_prefixes": [task["request"]["remote_root"]],
        "site_profile": task["request"]["site_profile"], "profile_revision": 2}
    context.worker.profiles = [profile_metadata("personal-test", protected)]
    context.worker.save(update_fields=["profiles"])
    config = resolve_connection({**context.connection.configuration, "remote_root": task["request"]["remote_root"]},
        {"personal-test": protected}, refresh=True)[1]
    assert reply(context, task, result={"ok": True, "path": task["request"]["remote_root"], "writable": True}, configuration=config).status_code == 200
    context.connection.refresh_from_db()
    assert context.connection.configuration["site_profile"]["cpu_allocation_quantum"] == 2
    assert context.connection.configuration_confirmed and context.connection.setup_complete
