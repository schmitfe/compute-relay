"""Generic Slurm GPU requests retain explicit human permission and current gates."""

from copy import deepcopy

import pytest

from compute_relay.control import services
from compute_relay.control.errors import BrokerError
from compute_relay.control.gpu_permissions import gpu_request_allowed
from compute_relay.control.models import Job
from compute_relay.control.resource_api import compute_context
from compute_relay.backends.discovery import parse_discovery
from compute_relay.domain.cluster_configuration import gpu_type_inventory
from test_support.unit_test_control import broker, prepared_job


def inventory(gres="gpu:a100:2,gpu:h200:2"):
    return {"node_inventory_complete": True, "nodes": [{"partition": "debug", "gres": gres}]}


@pytest.mark.parametrize("gres,complete,types", [
    ("gpu:a100:2,gpu:h200:2", True, ["a100", "h200"]),
    ("gpu:2,gpu:a100:2", True, ["a100"]),
    ("gpu:2", False, []),
    ("gpu:3,gpu:a100:2", False, []),
    ("gpu:a100:2,gpu:a100:2", False, []),
    (None, False, []),
    ("mps:100,shard:16", True, []),
])
def test_gpu_inventory_uses_strict_existing_gres_parser(gres, complete, types):
    assert gpu_type_inventory(inventory(gres), ["debug"]) == {"types": types, "complete": complete}


def test_gpu_inventory_requires_successful_discovery_and_each_selected_partition():
    snapshot = inventory()
    assert not gpu_type_inventory(snapshot, ["missing"])["complete"]
    assert not gpu_type_inventory(snapshot, ["debug", "missing"])["complete"]
    del snapshot["node_inventory_complete"]
    assert not gpu_type_inventory(snapshot, ["debug"])["complete"]


@pytest.mark.parametrize("returncode,stdout,complete", [
    (0, "node|debug|64|131072|gpu:a100:2|idle|2|16|2\n", True),
    (1, "node|debug|64|131072|gpu:a100:2|idle|2|16|2\n", False),
    (0, "node|debug|64|131072|gpu:a100:2|idle|2|16|2\nmalformed\n", False),
])
def test_discovery_does_not_label_failed_or_partial_node_output_complete(returncode, stdout, complete):
    snapshot = parse_discovery({"nodes": {"returncode": returncode, "stdout": stdout}}, {})
    assert snapshot["node_inventory_complete"] is complete
    assert gpu_type_inventory(snapshot, ["debug"])["complete"] is complete


@pytest.fixture
def gpu_project(broker):
    connection = broker.connection
    connection.configuration["site_profile"].update(partitions=["debug"], maximum_gpus=2)
    connection.discovery_snapshot = inventory()
    connection.save()
    broker.grant.policy.update(mode="budgeted", allowed_gpu_types=["a100", "h200"], allow_untyped_gpus=True)
    broker.grant.policy["per_job_limits"]["gpu_devices"] = 2
    broker.grant.save()
    return broker


def gpu_job(context, gpu_type=None):
    base = prepared_job(context)
    document = deepcopy(base.request)
    document["resources"]["gpus"] = 1
    if gpu_type:
        document["resources"]["gpu_type"] = gpu_type
    created, _ = services.create_job(context.credential, document)
    job = Job.objects.get(id=created["id"])
    services.prepare_job(job)
    job.refresh_from_db()
    return job


def test_all_selected_generic_job_prepares_and_admits_without_losing_reservation(gpu_project):
    context = gpu_project
    job = gpu_job(context)
    execution, _ = services.request_execution(job, context.credential)
    attempt = job.attempts.get(id=execution["attempt_id"])
    assert attempt.reservation_gpu == 60
    assert job.manifest["maximum_allocation"]["gpu_devices"] == 1
    context.grant.refresh_from_db()
    assert context.grant.reserved_gpu == 60
    assert compute_context(context.credential)["connections"][0]["untyped_gpu_partitions"] == ["debug"]


@pytest.mark.parametrize("change", ["intent", "partial", "empty", "unknown", "new_type"])
def test_generic_admission_rechecks_permission_and_discovery(gpu_project, change):
    context = gpu_project
    job = gpu_job(context)
    revoke(context, change)
    with pytest.raises(BrokerError, match="GPU type selection"):
        services.request_execution(job, context.credential)
    assert not job.attempts.exists()
    context.grant.refresh_from_db()
    assert context.grant.reserved_gpu == 0


def revoke(context, change):
    if change == "intent":
        context.grant.policy["allow_untyped_gpus"] = False
    elif change == "partial":
        context.grant.policy["allowed_gpu_types"] = ["a100"]
    elif change == "empty":
        context.grant.policy["allowed_gpu_types"] = []
        context.grant.policy["per_job_limits"]["gpu_devices"] = 0
    elif change == "unknown":
        context.connection.discovery_snapshot = inventory("gpu:4")
    else:
        context.connection.discovery_snapshot = inventory("gpu:a100:2,gpu:h200:2,gpu:rtx:1")
    context.grant.save()
    context.connection.save()


@pytest.mark.parametrize("change", ["intent", "partial", "unknown", "new_type"])
def test_generic_dispatch_rechecks_even_when_connection_revision_did_not_change(gpu_project, change):
    context = gpu_project
    job = gpu_job(context)
    services.request_execution(job, context.credential)
    lease = services.lease_work(context.worker)["lease"]
    revision = context.connection.revision_sha256
    revoke(context, change)
    assert context.connection.revision_sha256 == revision
    with pytest.raises(BrokerError, match="no longer authorizes"):
        services.dispatch(context.worker, lease["attempt_id"], {"lease_id": lease["id"], "generation": lease["generation"],
            "manifest_sha256": job.manifest_sha256, "connection_revision_sha256": revision})
    assert not job.attempts.get().dispatch_started


def test_partial_selection_does_not_become_generic_when_inventory_shrinks(gpu_project):
    context = gpu_project
    context.grant.policy.update(allowed_gpu_types=["a100"], allow_untyped_gpus=False)
    context.grant.save()
    context.connection.discovery_snapshot = inventory("gpu:a100:2")
    context.connection.save()
    with pytest.raises(BrokerError, match="Choose an allowed GPU type"):
        gpu_job(context)
    assert compute_context(context.credential)["connections"][0]["untyped_gpu_partitions"] == []


def test_explicit_allowed_types_still_work_with_incomplete_discovery(gpu_project):
    context = gpu_project
    revoke(context, "unknown")
    job = gpu_job(context, "a100")
    assert services.request_execution(job, context.credential)[0]["attempt_id"]
    disallowed = gpu_job(context, "rtx")
    with pytest.raises(BrokerError, match="GPU type selection"):
        services.request_execution(disallowed, context.credential)


def test_human_approval_cannot_override_partial_gpu_permission(gpu_project):
    context = gpu_project
    job = gpu_job(context)
    context.grant.policy["mode"] = "mixed"
    context.credential.execution_mode = "approval_required"
    context.credential.save()
    context.grant.save()
    execution, _ = services.request_execution(job, context.credential)
    revoke(context, "partial")
    with pytest.raises(BrokerError, match="GPU type selection"):
        services.decide(job, context.owner, {"execution_request_id": execution["id"],
            "manifest_sha256": job.manifest_sha256, "approved": True})
    assert not job.attempts.exists()


def test_gpu_allocation_floor_cannot_bypass_permission_as_cpu_only(gpu_project):
    context = gpu_project
    job = prepared_job(context)
    assert not gpu_request_allowed(context.grant.policy, context.connection, job.request, {"gpu_devices": 1})
