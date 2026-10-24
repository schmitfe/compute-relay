"""Requested-time reservations settle real usage without unconditional pauses."""

from copy import deepcopy

from django.db import transaction
import pytest

from compute_relay.control import services
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Attempt, ExecutionRequest, Job
from test_control import broker, budgeted, dispatched, prepared_job, submit_receipt  # noqa: F401


def settle(context, job, *, cpu_seconds):
    attempt, lease = dispatched(context, job)
    native, lease = submit_receipt(context, attempt, lease)
    response = services.report(context.worker, attempt.id, {
        "lease_id": lease["id"], "generation": lease["generation"], "outcome": "observed",
        "native_identity": native, "observation": {"state": "completed", "terminal": True,
            "accounting": {"cpu_core_seconds": cpu_seconds, "gpu_device_seconds": 0, "final": True}}})
    return attempt, response


def test_overrun_with_remaining_allowance_can_admit_another_job(broker):
    budgeted(broker, cpu=240, gpu=0)
    job = prepared_job(broker)
    assert job.manifest["reservation"]["cpu_core_seconds"] == 60
    attempt, _ = settle(broker, job, cpu_seconds=120)
    attempt.refresh_from_db()
    broker.project.refresh_from_db()
    assert attempt.error_code == "OBSERVED_OVERAGE"
    assert not broker.project.paused
    assert services.allowance(broker.project)["available"]["cpu_core_seconds"] == 120
    with transaction.atomic():
        result, _ = services.request_execution(job, broker.credential)
    assert result["attempt_id"]
    broker.grant.refresh_from_db()
    assert (broker.grant.spent_cpu, broker.grant.reserved_cpu) == (120, 60)


@pytest.mark.parametrize("actual, allowed", [(120, True), (181, False)])
def test_queued_job_rechecks_actual_usage_before_first_dispatch(broker, actual, allowed):
    budgeted(broker, cpu=240, gpu=0)
    job = prepared_job(broker)
    first, lease = dispatched(broker, job)
    native, observed_lease = submit_receipt(broker, first, lease)
    with transaction.atomic():
        queued, _ = services.request_execution(job, broker.credential)
    queued_attempt = Attempt.objects.get(id=queued["attempt_id"])
    services.report(broker.worker, first.id, {"lease_id": observed_lease["id"],
        "generation": observed_lease["generation"], "outcome": "observed", "native_identity": native,
        "observation": {"state": "completed", "terminal": True,
            "accounting": {"cpu_core_seconds": actual, "gpu_device_seconds": 0, "final": True}}})
    next_lease = services.lease_work(broker.worker)["lease"]
    assert next_lease["attempt_id"] == str(queued_attempt.id)
    document = {"lease_id": next_lease["id"], "generation": next_lease["generation"],
        "manifest_sha256": job.manifest_sha256, "connection_revision_sha256": broker.connection.revision_sha256}
    if allowed:
        assert services.dispatch(broker.worker, queued_attempt.id, document)["authorized"]
    else:
        with pytest.raises(BrokerError) as error:
            services.dispatch(broker.worker, queued_attempt.id, document)
        assert error.value.code == "BUDGET_UNAVAILABLE"
        queued_attempt.refresh_from_db()
        assert not queued_attempt.dispatch_started and queued_attempt.native_identity is None
    broker.project.refresh_from_db()
    assert not broker.project.paused


def test_longer_walltime_increases_the_reservation(broker):
    budgeted(broker, cpu=10000, gpu=0)
    first = prepared_job(broker)
    request = deepcopy(first.request)
    request["resources"]["time_limit_seconds"] = 120
    with transaction.atomic():
        created, _ = services.create_job(broker.credential, request)
    job = Job.objects.get(id=created["id"])
    services.prepare_job(job)
    job.refresh_from_db()
    assert job.manifest["maximum_allocation"]["time_limit_seconds"] == 120
    assert job.manifest["reservation"]["cpu_core_seconds"] == 120
    with transaction.atomic():
        result, _ = services.request_execution(job, broker.credential)
    assert ExecutionRequest.objects.get(id=result["id"]).state == "authorized"


def test_reservation_uses_only_configured_walltime(broker):
    broker.connection.configuration["site_profile"] = {
        "partitions": ["debug"], "accounts": ["broker"]}
    broker.connection.save(update_fields=["configuration"])
    job = prepared_job(broker)
    assert job.manifest["maximum_allocation"] == {"cpu_cores": 1, "gpu_devices": 0, "time_limit_seconds": 60}
    assert job.manifest["reservation"] == {"cpu_core_seconds": 60, "gpu_device_seconds": 0}
