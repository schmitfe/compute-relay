"""The authenticated HTTP poll path prioritizes live jobs over setup work."""
from test_support.unit_test_lease_priority_http import worker_client

import json
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.models import Attempt
from test_support.unit_test_control import broker, budgeted, execution, prepared_job




def test_pending_cancel_bypasses_old_paused_submissions_and_setup_queues(broker):
    budgeted(broker)
    job = prepared_job(broker)
    for _ in range(35):
        execution(broker, job)
    running = execution(broker, job).attempt
    Attempt.objects.filter(id=running.id).update(state="running", dispatch_started=True,
        native_identity={"job_id": "17", "attempt_id": str(running.id)}, cancel_requested=True)
    broker.project.paused = True
    broker.project.save(update_fields=["paused"])
    broker.connection.discovery_state = "queued"
    broker.connection.save(update_fields=["discovery_state"])
    with patch("compute_relay.extensions.before_lease", side_effect=AssertionError("Setup must wait")), \
            patch("compute_relay.control.cluster_services.lease_discovery", side_effect=AssertionError("Discovery must wait")):
        response = worker_client().post("/internal/v1/lease", data="{}", content_type="application/json")
    assert response.status_code == 200
    lease = response.json()["lease"]
    assert lease["attempt_id"] == str(running.id) and lease["operation"] == "cancel"


def test_project_bearer_cannot_renew_worker_leases(broker):
    budgeted(broker)
    job = prepared_job(broker)
    attempt = execution(broker, job).attempt
    lease = services.lease_work(broker.worker)["lease"]
    response = Client(HTTP_AUTHORIZATION="Bearer " + broker.token).post(
        f"/internal/v1/attempts/{attempt.id}/renew",
        data=json.dumps({"lease_id": lease["id"], "generation": lease["generation"]}),
        content_type="application/json")
    assert response.status_code in {401, 403}
    attempt.refresh_from_db()
    assert attempt.lease_expires_at > timezone.now()


def test_renewal_rejects_replaced_generation_through_http(broker):
    budgeted(broker)
    job = prepared_job(broker)
    attempt = execution(broker, job).attempt
    lease = services.lease_work(broker.worker)["lease"]
    Attempt.objects.filter(id=attempt.id).update(generation=lease["generation"] + 1)
    response = worker_client().post(f"/internal/v1/attempts/{attempt.id}/renew",
        data=json.dumps({"lease_id": lease["id"], "generation": lease["generation"]}),
        content_type="application/json")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_LEASE"


def test_running_polls_cannot_starve_eight_pending_submissions_under_ten_job_cap(broker, monkeypatch):
    from datetime import timedelta
    budgeted(broker, cpu=100000)
    broker.grant.policy["max_active_jobs"] = 10
    broker.grant.save()
    job = prepared_job(broker)
    attempts = [execution(broker, job).attempt for _ in range(10)]
    start = timezone.now()
    clock = [start]
    for index, attempt in enumerate(attempts):
        Attempt.objects.filter(id=attempt.id).update(next_poll_at=start)
        if index < 2:
            Attempt.objects.filter(id=attempt.id).update(state="running", dispatch_started=True,
                native_identity={"job_id": str(17 + index), "attempt_id": str(attempt.id)},
                next_poll_at=start - timedelta(seconds=1))
    monkeypatch.setattr(services.timezone, "now", lambda: clock[0])
    client = worker_client()
    submissions = []
    for _ in range(20):
        response = client.post("/internal/v1/lease", data="{}", content_type="application/json")
        assert response.status_code == 200, response.content
        lease = response.json()["lease"]
        assert lease is not None
        receipt = {"lease_id": lease["id"], "generation": lease["generation"]}
        clock[0] += timedelta(seconds=3)  # Slower than the old 2-second polling delay.
        if lease["operation"] == "submit":
            confirmed = client.post(f"/internal/v1/attempts/{lease['attempt_id']}/dispatch", data=json.dumps({**receipt,
                "manifest_sha256": job.manifest_sha256, "connection_revision_sha256": broker.connection.revision_sha256}), content_type="application/json")
            assert confirmed.status_code == 200, confirmed.content
            receipt.update(outcome="submitted", native_identity={"job_id": str(100 + len(submissions)), "attempt_id": lease["attempt_id"]})
            submissions.append(lease["attempt_id"])
        else:
            assert lease["operation"] == "observe"
            receipt.update(outcome="observed", native_identity=lease["native_identity"], observation={"state": "running", "terminal": False})
        reported = client.post(f"/internal/v1/attempts/{lease['attempt_id']}/report", data=json.dumps(receipt), content_type="application/json")
        assert reported.status_code == 200, reported.content
        clock[0] += timedelta(seconds=1)
        if len(submissions) == 8:
            break
    assert set(submissions) == {str(attempt.id) for attempt in attempts[2:]}
    assert Attempt.objects.filter(native_identity__isnull=False).count() == 10


@pytest.mark.parametrize("document", [[], {"core_only": 1}, {"unknown": True},
    {"in_progress": "not-a-list"}, {"in_progress": [None]}, {"in_progress": [17]},
    {"in_progress": ["invalid"]}, {"in_progress": ["0" * 32] * 9}])
def test_invalid_poll_options_do_not_acquire_a_lease(broker, document):
    budgeted(broker)
    attempt = execution(broker, prepared_job(broker)).attempt
    response = worker_client().post("/internal/v1/lease", data=json.dumps(document), content_type="application/json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    attempt.refresh_from_db()
    assert attempt.generation == 0


def test_busy_poll_filters_in_progress_and_does_not_lease_discovery(broker):
    budgeted(broker)
    job = prepared_job(broker)
    active, pending = [execution(broker, job).attempt for _ in range(2)]
    broker.connection.discovery_state = "queued"; broker.connection.save()
    client = worker_client()
    response = client.post("/internal/v1/lease", data=json.dumps({"core_only": True, "in_progress": [str(active.id)]}), content_type="application/json")
    assert response.status_code == 200
    assert response.json()["lease"]["attempt_id"] == str(pending.id)
    assert client.post("/internal/v1/lease", data=json.dumps({"core_only": True, "in_progress": [str(active.id)]}), content_type="application/json").json() == {"lease": None}
    active.refresh_from_db()
    assert active.generation == 0
    broker.connection.refresh_from_db()
    assert broker.connection.discovery_state == "queued"
