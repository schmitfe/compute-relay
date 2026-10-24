"""The authenticated HTTP poll path prioritizes live jobs over setup work."""

import json
from unittest.mock import patch

from django.test import Client
from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.models import Attempt
from test_control import broker, budgeted, execution, prepared_job  # noqa: F401


def worker_client():
    return Client(HTTP_AUTHORIZATION="Bearer synthetic-worker-token")


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
