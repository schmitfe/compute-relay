"""A stopped worker must not look like a fresh scheduler observation."""

from datetime import timedelta

from django.test import Client
from django.utils import timezone
import pytest

from compute_relay.control.models import Attempt, ExecutionRequest
from compute_relay.mcp.responses import compact_job
from test_support.unit_test_control import broker, prepared_job
from compute_relay.control.worker_status import worker_contact


def running(context, job):
    execution = ExecutionRequest.objects.create(job=job, credential=context.credential, state="authorized")
    return Attempt.objects.create(job=job, execution_request=execution, connection=context.connection,
        grant=context.grant, state="running", accounting_state="reserved", reservation_cpu=60, reservation_gpu=0,
        dispatch_started=True, native_identity={"job_id": "197895"},
        observation={"state": "running", "native_state": "RUNNING", "source": "squeue"})


def contact(context, timestamp):
    context.worker.last_seen_at = timestamp
    context.worker.save(update_fields=["last_seen_at"])


def test_stopped_worker_is_visible_through_rest_mcp_and_live_portal_then_recovers(broker):
    job = prepared_job(broker)
    attempt = running(broker, job)
    contact(broker, timezone.now() - timedelta(hours=10))
    api = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    human = Client()
    human.force_login(broker.owner)
    path = f"/api/v1/jobs/{job.id}"
    old = api.get(path).json()
    assert old["attempts"][0]["tracking"]["status"] == "stale"
    compact = compact_job(old)
    assert compact["latest_attempt"]["state"] == "running"
    assert compact["next_action"] == {"action": "reconnect_connector", "requires_human": True}
    assert "last recorded" in human.get("/ui/live/dashboard/").json()["fragments"]["live-job-list"].lower()
    review = human.get(f"/ui/live/review/{job.id}/").json()["attempts"][0]["html"]
    assert "Connector is not reporting" in review and "may have finished" in review
    cluster = human.get(f"/ui/live/cluster/{broker.connection.id}/").json()["fragments"]["live-cluster-status"]
    assert "Connector is not reporting" in cluster
    assert f'/clusters/{broker.connection.id}/#connector' in cluster

    contact(broker, timezone.now())
    fresh = api.get(path, {"after_revision": old["revision"]}).json()
    assert fresh["changed"] and fresh["attempts"][0]["tracking"]["status"] == "recent"
    assert compact_job(fresh)["next_action"]["action"] == "wait"
    assert "Connector is not reporting" not in human.get(f"/ui/live/cluster/{broker.connection.id}/").json()["fragments"]["live-cluster-status"]
    attempt.refresh_from_db()
    assert attempt.state == "running" and attempt.accounting_state == "reserved" and attempt.reservation_cpu == 60
    assert job.attempts.count() == 1


def test_finished_collected_job_does_not_require_a_connected_worker(broker):
    job = prepared_job(broker)
    attempt = running(broker, job)
    attempt.state, attempt.accounting_state, attempt.collection_state = "succeeded", "settled", "complete"
    attempt.save(update_fields=["state", "accounting_state", "collection_state"])
    contact(broker, timezone.now() - timedelta(hours=10))
    api = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    response = api.get(f"/api/v1/jobs/{job.id}").json()
    assert response["attempts"][0]["tracking"] is None
    assert compact_job(response)["next_action"]["action"] == "inspect_results"


@pytest.mark.parametrize("seconds,status", [(0, "recent"), (300, "recent"), (301, "stale"), (None, "not_seen")])
def test_contact_freshness_uses_a_stable_boundary_without_claiming_ssh_connectivity(seconds, status):
    now = timezone.now()
    seen = now - timedelta(seconds=seconds) if seconds is not None else None
    assert worker_contact(seen, now=now) == {"status": status, "last_contact_at": seen.isoformat() if seen else None}
    assert worker_contact(seen, revoked=True, now=now)["status"] == "revoked"


def test_stale_worker_still_matters_after_execution_until_collection_finishes(broker):
    job = prepared_job(broker)
    attempt = running(broker, job)
    attempt.state, attempt.accounting_state = "succeeded", "settled"
    attempt.collection_pending, attempt.collection_state = True, "retry_pending"
    attempt.save(update_fields=["state", "accounting_state", "collection_pending", "collection_state"])
    contact(broker, timezone.now() - timedelta(hours=10))
    api = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    value = api.get(f"/api/v1/jobs/{job.id}").json()
    assert value["attempts"][0]["state"] == "succeeded"
    assert compact_job(value)["next_action"]["action"] == "reconnect_connector"


def test_off_page_worker_recovery_changes_the_completion_revision_without_changing_jobs(broker):
    job = prepared_job(broker)
    finished = running(broker, job)
    finished.state, finished.accounting_state, finished.collection_state = "succeeded", "settled", "complete"
    finished.save(update_fields=["state", "accounting_state", "collection_state"])
    running(broker, job)
    contact(broker, timezone.now() - timedelta(hours=10))
    api = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    path = f"/api/v1/jobs/{job.id}/completion"
    old = api.get(path, {"cursor": 0, "limit": 1}).json()
    contact(broker, timezone.now())
    new = api.get(path, {"cursor": 0, "limit": 1}).json()
    assert new["attempts"] == old["attempts"]
    assert new["snapshot_revision"] != old["snapshot_revision"]


def test_removed_cluster_does_not_suggest_reconnecting_an_archived_configuration(broker):
    job = prepared_job(broker)
    running(broker, job)
    broker.connection.archived = True
    broker.connection.save(update_fields=["archived"])
    human = Client()
    human.force_login(broker.owner)
    review = human.get(f"/ui/live/review/{job.id}/").json()["attempts"][0]["html"]
    assert "Cluster removed with unresolved work" in review
    assert f'/clusters/{broker.connection.id}/#connector' not in review
