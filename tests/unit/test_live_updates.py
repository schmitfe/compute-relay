"""Human portal polling refreshes saved facts without granting compute authority."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from compute_broker.control import services
from compute_broker.control.models import ApprovalDecision, Attempt, Connection, ExecutionRequest, Job, Membership
from test_control import broker, execution, prepared_job


def human(user):
    client = Client()
    client.force_login(user)
    return client


def live(client, kind="dashboard", identifier=None):
    return client.get(f"/ui/live/{kind}/{identifier}/" if identifier else f"/ui/live/{kind}/")


def attempt(context, job, *, state="running", accounting_state="reserved", created_at=None):
    request = ExecutionRequest.objects.create(job=job, credential=context.credential, state="authorized")
    return Attempt.objects.create(execution_request=request, job=job, connection=context.connection,
        grant=context.grant, reservation_cpu=60, reservation_gpu=0, state=state,
        accounting_state=accounting_state, created_at=created_at or timezone.now())


@pytest.mark.parametrize("kind", ["dashboard", "project", "review", "cluster"])
def test_live_updates_require_a_human_session_instead_of_agent_bearer_key(broker, kind):
    identifier = {"dashboard": None, "project": broker.project.id,
        "review": uuid4(), "cluster": broker.connection.id}[kind]
    response = live(Client(HTTP_AUTHORIZATION="Bearer " + broker.token), kind, identifier)
    assert response.status_code == 401
    assert response["Cache-Control"] == "no-store, private"
    assert response["Vary"] == "Cookie"
    assert "fragments" not in response.json()


def test_snapshots_accept_only_get_and_a_fixed_allowlist_of_page_kinds(broker):
    client = human(broker.owner)
    response = client.post("/ui/live/dashboard/", {})
    assert response.status_code == 405 and response["Allow"] == "GET"
    assert live(client, "template", broker.project.id).status_code == 404
    assert client.get("/ui/live/dashboard/", {"template": "broker/key_created.html"}).status_code == 200


def test_dashboard_updates_job_state_and_new_pending_review_notice(broker):
    job = prepared_job(broker)
    client = human(broker.owner)
    initial = live(client).json()["fragments"]
    assert "Prepared" in initial["live-job-list"]
    assert "No execution requests" in initial["live-review-notifications"]
    request = execution(broker, job)
    queued = live(client).json()["fragments"]
    assert "Awaiting approval" in queued["live-job-list"]
    assert "1 execution request needs a decision" in queued["live-review-notifications"]
    assert f'/review/{job.id}/' in queued["live-review-notifications"]
    Attempt.objects.create(execution_request=request, job=job, connection=broker.connection,
        grant=broker.grant, reservation_cpu=60, reservation_gpu=0, state="running")
    request.state = "authorized"
    request.save(update_fields=["state"])
    running = live(client).json()["fragments"]
    assert "Running" in running["live-job-list"]
    assert "No execution requests" in running["live-review-notifications"]


def test_dashboard_membership_and_approval_roles_are_checked_on_each_poll(broker):
    job = prepared_job(broker)
    execution(broker, job)
    membership = Membership.objects.create(project=broker.project, user=broker.outsider, roles=["viewer"])
    client = human(broker.outsider)
    viewed = live(client).json()["fragments"]
    assert f'/review/{job.id}/' in viewed["live-job-list"]
    assert "No execution requests" in viewed["live-review-notifications"]
    membership.roles = ["approver"]
    membership.save(update_fields=["roles"])
    assert "1 execution request" in live(client).json()["fragments"]["live-review-notifications"]
    membership.delete()
    withdrawn = live(client).json()["fragments"]
    assert str(job.id) not in withdrawn["live-job-list"]
    assert "No execution requests" in withdrawn["live-review-notifications"]


def test_project_snapshot_updates_shared_allowance_and_requires_current_admin_role(broker):
    job = prepared_job(broker)
    attempt(broker, job)
    membership = Membership.objects.create(project=broker.project, user=broker.outsider, roles=["project_admin"])
    client = human(broker.outsider)
    response = live(client, "project", broker.project.id)
    assert response.status_code == 200
    assert "1 of 100 simultaneous jobs" in response.json()["fragments"]["live-project-allowance"]
    membership.roles = ["viewer"]
    membership.save(update_fields=["roles"])
    assert live(client, "project", broker.project.id).status_code == 403


def test_review_snapshot_inserts_a_new_bound_approval_panel_without_reloading_source(broker):
    job = prepared_job(broker)
    client = human(broker.owner)
    initial = live(client, "review", job.id)
    assert initial.status_code == 200 and initial.json()["execution_requests"] == []
    request = execution(broker, job)
    with patch("compute_broker.control.services.read_blob", side_effect=AssertionError("Polling must not read source files")):
        response = live(client, "review", job.id)
    payload = response.json()
    panel, = payload["execution_requests"]
    assert panel["id"] == str(request.id) and panel["state"] == "awaiting_approval"
    assert f'data-live-review-request="{request.id}"' in panel["html"]
    assert f'action="/review/{job.id}/"' in panel["html"]
    assert 'name="csrfmiddlewaretoken"' in panel["html"]
    assert job.manifest_sha256 in panel["html"] and "Approve one execution" in panel["html"]
    assert "Awaiting approval" in payload["fragments"]["live-job-summary"]
    assert broker.token not in response.content.decode()
    assert "uploaded source" not in response.content.decode()


def test_review_snapshot_keeps_an_older_active_attempt_updating_among_many_finished_attempts(broker):
    job = prepared_job(broker)
    old = attempt(broker, job, created_at=timezone.now() - timedelta(days=1))
    for index in range(12):
        attempt(broker, job, state="succeeded", accounting_state="settled",
            created_at=timezone.now() - timedelta(minutes=index))
    payload = live(human(broker.owner), "review", job.id).json()
    assert len(payload["attempts"]) == 11
    assert str(old.id) in [item["id"] for item in payload["attempts"]]
    old.state = "failed"
    old.stderr = "Synthetic failure after pending allocation"
    old.observation = {"native_state": "FAILED", "exit_code": "1:0"}
    old.save(update_fields=["state", "stderr", "observation"])
    updated = live(human(broker.owner), "review", job.id).json()
    details = next(item["html"] for item in updated["attempts"] if item["id"] == str(old.id))
    assert 'data-state="failed"' in details and "Synthetic failure" in details


def test_review_snapshot_bounds_history_queries_but_includes_old_pending_and_recent_decisions(broker):
    job = prepared_job(broker)
    start = timezone.now() - timedelta(days=1)
    requests = [ExecutionRequest(job=job, credential=broker.credential,
        created_at=start + timedelta(seconds=index)) for index in range(250)]
    ExecutionRequest.objects.bulk_create(requests)
    requests[0].state = "rejected"
    requests[0].save(update_fields=["state"])
    ApprovalDecision.objects.create(execution_request=requests[0], approver=broker.owner,
        approved=False, manifest_sha256=job.manifest_sha256, comment="New decision on an older request")
    client = human(broker.owner)
    with patch("compute_broker.control.services.serialize_job", side_effect=AssertionError("No full-history serialization")):
        with CaptureQueriesContext(connection) as queries:
            response = live(client, "review", job.id)
    assert response.status_code == 200
    assert len(queries) <= 20
    panels = {item["id"]: item for item in response.json()["execution_requests"]}
    assert len(panels) <= 200
    assert str(requests[1].id) in panels and str(requests[-1].id) in panels
    assert panels[str(requests[0].id)]["state"] == "rejected"
    assert "New decision on an older request" in panels[str(requests[0].id)]["html"]


def test_review_snapshot_rechecks_approver_membership_and_owner_global_suspension(broker):
    job = prepared_job(broker)
    membership = Membership.objects.create(project=broker.project, user=broker.outsider, roles=["approver"])
    client = human(broker.outsider)
    assert live(client, "review", job.id).status_code == 200
    membership.delete()
    assert live(client, "review", job.id).status_code == 403
    broker.owner.is_active = False
    broker.owner.save(update_fields=["is_active"])
    assert live(human(broker.owner), "review", job.id).status_code in {401, 403}


def test_cluster_poll_updates_saved_discovery_and_revocation_without_exposing_credentials(broker):
    client = human(broker.owner)
    assert live(client, "cluster", broker.connection.id).status_code == 200
    broker.connection.discovery_state = "running"
    broker.connection.configuration["private"] = "PRIVATE-SSH-DETAIL"
    broker.connection.save(update_fields=["discovery_state", "configuration"])
    broker.worker.revoked = True
    broker.worker.save(update_fields=["revoked"])
    response = live(client, "cluster", broker.connection.id)
    html = response.json()["fragments"]["live-cluster-status"]
    assert "running" in html and "Revoked" in html and "PRIVATE-" not in html
    assert live(human(broker.outsider), "cluster", broker.connection.id).status_code == 404


def test_all_snapshot_queries_are_read_only_and_do_not_contact_execution_paths(broker):
    job = prepared_job(broker)
    execution(broker, job)
    client = human(broker.owner)
    with patch("compute_broker.control.services.prepare_job", side_effect=AssertionError("No preparation")), \
            patch("compute_broker.control.services.authorize", side_effect=AssertionError("No authorization")), \
            patch("compute_broker.control.services.lease_work", side_effect=AssertionError("No worker query")):
        with CaptureQueriesContext(connection) as queries:
            responses = [live(client), live(client, "project", broker.project.id),
                live(client, "review", job.id), live(client, "cluster", broker.connection.id)]
    assert all(response.status_code == 200 for response in responses)
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    assert not Attempt.objects.exists()
    assert services.allowance(broker.project)["reserved"] == {"cpu_core_seconds": 0, "gpu_device_seconds": 0}


def test_job_and_review_content_is_escaped_in_fixed_server_fragments(broker):
    job = prepared_job(broker)
    job.request["name"] = '<img src=x onerror="alert(1)">'
    job.save(update_fields=["request"])
    execution(broker, job)
    fragments = live(human(broker.owner)).json()["fragments"]
    assert "<img" not in fragments["live-job-list"] and "&lt;img" in fragments["live-job-list"]
    assert "<img" not in fragments["live-review-notifications"]


def test_draft_review_snapshot_has_no_package_validation_or_execution_authority(broker):
    prepared = prepared_job(broker)
    created, _ = services.create_job(broker.credential, prepared.request)
    response = live(human(broker.owner), "review", created["id"])
    assert response.status_code == 200
    assert response.json()["execution_requests"] == response.json()["attempts"] == []
    assert "draft" in response.json()["fragments"]["live-job-summary"]


def test_pages_enable_fragment_polling_without_marking_settings_or_package_as_replaceable(broker):
    job = prepared_job(broker)
    client = human(broker.owner)
    pages = [client.get("/"), client.get(f"/projects/{broker.project.id}/"),
        client.get(f"/review/{job.id}/"), client.get(f"/clusters/{broker.connection.id}/")]
    assert all(page.status_code == 200 for page in pages)
    assert all(b"data-live-page" in page.content and b"const normalDelay = 4000" in page.content for page in pages)
    review = pages[2].content.decode()
    assert "data-live-executions" in review and "data-live-attempts" in review
    assert "Your unsent comment is preserved" in review
    assert "event.persisted" in review and "document.hidden" in review
    assert 'redirect: \'error\'' in review and "cache: 'no-store'" in review
