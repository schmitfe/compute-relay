"""Controller fairness, payload bounds and independent batch receipts."""
from unittest.mock import patch
from compute_relay.control.errors import BrokerError


from datetime import timedelta
import json
import pytest

from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.models import Attempt
from test_support.unit_test_control import broker, budgeted, execution, prepared_job
from test_support.unit_test_lease_priority_http import worker_client


def running(attempt, when):
    Attempt.objects.filter(id=attempt.id).update(state="running", dispatch_started=True, next_poll_at=when,
        native_identity={"job_id": str(attempt.pk.int % 1000000000), "attempt_id": str(attempt.id)})


def test_five_hundred_attempts_lease_in_bounded_groups_and_settle_independently(broker):
    budgeted(broker, cpu=100000)
    broker.grant.policy["max_active_jobs"] = 1000; broker.grant.save()
    job = prepared_job(broker)
    attempts = [execution(broker, job).attempt for _ in range(500)]
    due = timezone.now() - timedelta(seconds=1)
    for attempt in attempts:
        running(attempt, due)
    client = worker_client()
    seen, groups = set(), 0
    while len(seen) < 500:
        response = client.post("/internal/v1/lease", data=json.dumps({}), content_type="application/json")
        assert response.status_code == 200
        value = response.json()
        leases = [value["lease"], *value.get("observations", [])]
        assert 1 <= len(leases) <= 32
        assert len(response.content) <= 8 * 1024 * 1024
        groups += 1
        for lease in leases:
            assert lease["attempt_id"] not in seen
            seen.add(lease["attempt_id"])
            report = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "observed",
                "native_identity": lease["native_identity"], "observation": {"state": "completed", "terminal": True,
                    "accounting": {"final": True, "cpu_core_seconds": 1, "gpu_device_seconds": 0}}}
            response = client.post(f"/internal/v1/attempts/{lease['attempt_id']}/report", data=json.dumps(report), content_type="application/json")
            assert response.status_code == 200, response.content
    assert groups == 16
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 0 and broker.grant.spent_cpu == 500


def test_status_batch_stops_at_an_older_submission_and_does_not_delay_its_turn(broker):
    budgeted(broker)
    job = prepared_job(broker)
    first, submission, last = [execution(broker, job).attempt for _ in range(3)]
    now = timezone.now()
    running(first, now - timedelta(seconds=3))
    Attempt.objects.filter(id=submission.id).update(next_poll_at=now - timedelta(seconds=2))
    running(last, now - timedelta(seconds=1))
    value = services.lease_work(broker.worker)
    assert value["lease"]["attempt_id"] == str(first.id) and "observations" not in value
    value = services.lease_work(broker.worker)
    assert value["lease"]["attempt_id"] == str(submission.id) and value["lease"]["operation"] == "submit"


def test_batch_refuses_to_skip_over_cancellation(broker):
    budgeted(broker)
    job = prepared_job(broker)
    first, cancelling, last = [execution(broker, job).attempt for _ in range(3)]
    now = timezone.now()
    for index, attempt in enumerate((first, cancelling, last)):
        running(attempt, now - timedelta(seconds=3 - index))
    Attempt.objects.filter(id=cancelling.id).update(cancel_requested=True)
    assert "observations" not in services.lease_work(broker.worker)
    assert services.lease_work(broker.worker)["lease"]["operation"] == "cancel"


@pytest.mark.parametrize("size", [0, 1, 32, 65, True, "32", 1.0])
def test_removed_batch_option_does_not_acquire_any_lease(broker, size):
    budgeted(broker)
    attempt = execution(broker, prepared_job(broker)).attempt
    response = worker_client().post("/internal/v1/lease", data=json.dumps({"observation_batch": size}), content_type="application/json")
    assert response.status_code == 400
    attempt.refresh_from_db()
    assert attempt.generation == 0


def test_cancellation_arriving_during_observation_does_not_inherit_poll_backoff(broker):
    budgeted(broker)
    attempt = execution(broker, prepared_job(broker)).attempt
    running(attempt, timezone.now() - timedelta(seconds=1))
    Attempt.objects.filter(id=attempt.id).update(generation=20)
    lease = services.lease_work(broker.worker)["lease"]
    services.cancel_attempt(attempt)
    services.report(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "outcome": "observed", "native_identity": lease["native_identity"],
        "observation": {"state": "running", "terminal": False}})
    assert services.lease_work(broker.worker)["lease"]["operation"] == "cancel"


@pytest.mark.parametrize("failure", [FileNotFoundError(), BrokerError("OBJECT_INTEGRITY", "corrupt", 409),
    RuntimeError("Unexpected extension failure")])
def test_bad_first_candidate_does_not_starve_worker(broker, failure):
    budgeted(broker)
    broken_job, healthy_job = prepared_job(broker), prepared_job(broker)
    broken = execution(broker, broken_job).attempt
    healthy = execution(broker, healthy_job).attempt
    missing = broken_job.files.get(path="main.py").blob_id
    original = services.read_blob
    def read(blob):
        if blob.id == missing:
            raise failure
        return original(blob)
    # These jobs deliberately share content; make only the first candidate fail.
    original_lease = services._lease_attempt
    def lease(attempt, entries, now):
        if attempt.id == broken.id:
            with patch.object(services, "read_blob", side_effect=read):
                return original_lease(attempt, entries, now)
        return original_lease(attempt, entries, now)
    with patch.object(services, "_lease_attempt", side_effect=lease):
        result = services.lease_work(broker.worker)
    assert result["lease"]["attempt_id"] == str(healthy.id)
    broken.refresh_from_db()
    assert broken.error_code and broken.next_poll_at > timezone.now()
    assert broken.lease_expires_at is None and broken.generation == 0


@pytest.mark.parametrize("cancel", [False, True])
def test_management_lease_does_not_read_inputs(broker, cancel):
    budgeted(broker)
    attempt = execution(broker, prepared_job(broker)).attempt
    Attempt.objects.filter(id=attempt.id).update(dispatch_started=True, native_identity={"job_id": "17"}, cancel_requested=cancel)
    with patch.object(services, "read_blob", side_effect=FileNotFoundError()):
        lease = services.lease_work(broker.worker)["lease"]
    assert lease["operation"] == ("cancel" if cancel else "observe")
    assert lease["prepared"]["files"] == {}
