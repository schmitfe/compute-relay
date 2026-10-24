"""Waiting reviews share project funds and become one safely charged execution."""

from copy import deepcopy
from datetime import timedelta
import json

import pytest
from django.db import transaction
from django.test import Client
from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.auth import token_hash
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import (ApprovalDecision, Attempt, ExecutionRequest, Grant, Job,
    LedgerEntry, Project, ProjectCredential, User)
from test_control import broker, budgeted, decide, execution, prepared_job, submit_receipt


def policy(context, **changes):
    context.grant.refresh_from_db()
    context.grant.policy = {**deepcopy(context.grant.policy), **changes}
    context.grant.save(update_fields=["policy"])


def allowance(context, cpu=60, gpu=0):
    policy(context, allowance={"cpu_core_seconds": cpu, "gpu_device_seconds": gpu})


def another_key(context, token="another-pending-review-key", **changes):
    return ProjectCredential.objects.create(project=context.project, issued_by=context.owner, name="Independent agent",
        prefix="pending-other", token_hash=token_hash(token), scopes=list(context.credential.scopes),
        expires_at=timezone.now() + timedelta(days=1), **changes)


def gpu_job(context):
    document = deepcopy(prepared_job(context).request)
    document["resources"].update(gpus=1, gpu_type="a100")
    with transaction.atomic():
        result, _ = services.create_job(context.credential, document)
    job = Job.objects.get(id=result["id"])
    services.prepare_job(job)
    job.refresh_from_db()
    return job


def cancel_url(job, request):
    return f"/api/v1/jobs/{job.id}/execution-requests/{request.id}/cancel"


def api_post(context, path, *, token=None, key="pending-request", document=None):
    return Client().post(path, json.dumps({} if document is None else document), content_type="application/json",
        HTTP_AUTHORIZATION="Bearer " + (token or context.token), HTTP_IDEMPOTENCY_KEY=key)


def test_waiting_approval_holds_shared_funds_without_using_scheduler_admission_slots(broker):
    allowance(broker, 120)
    policy(broker, max_active_jobs=1, max_total_jobs=1)
    job = prepared_job(broker)
    first = execution(broker, job)
    second = execution(broker, job, another_key(broker))
    snapshot = services.allowance(broker.project)
    assert first.reservation_state == second.reservation_state == "held"
    assert (first.reservation_cpu, first.reservation_gpu) == (60, 0)
    assert snapshot["reserved"] == snapshot["reserved_awaiting_approval"] == {"cpu_core_seconds": 120, "gpu_device_seconds": 0}
    assert snapshot["available"]["cpu_core_seconds"] == 0 and snapshot["spent"]["cpu_core_seconds"] == 0
    assert snapshot["jobs"]["awaiting_approval"] == 2
    assert snapshot["jobs"]["active"] == snapshot["jobs"]["admitted"] == 0
    assert snapshot["jobs"]["remaining"] == 1 and not Attempt.objects.exists()
    decide(broker, job, first)
    with pytest.raises(BrokerError) as denied:
        decide(broker, job, second)
    assert denied.value.code == "TOTAL_JOB_LIMIT"
    assert not ApprovalDecision.objects.filter(execution_request=second).exists()
    second.refresh_from_db()
    assert second.reservation_state == "held"


def test_different_project_keys_cannot_overpromise_the_last_pending_funds(broker):
    allowance(broker)
    job = prepared_job(broker)
    execution(broker, job)
    with pytest.raises(BrokerError) as denied:
        execution(broker, job, another_key(broker))
    assert denied.value.code == "BUDGET_UNAVAILABLE"
    assert ExecutionRequest.objects.count() == LedgerEntry.objects.filter(action="hold").count() == 1
    assert not Attempt.objects.exists()
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 60


def test_review_rejection_releases_its_hold_once_and_preserves_feedback(broker):
    allowance(broker)
    job = prepared_job(broker)
    pending = execution(broker, job)
    decide(broker, job, pending, approved=False, comment="Use a smaller ensemble.")
    services.cancel_execution_request(pending)
    with pytest.raises(BrokerError):
        decide(broker, job, pending, approved=False)
    pending.refresh_from_db()
    broker.grant.refresh_from_db()
    assert pending.state == "rejected" and pending.reservation_state == "released"
    assert broker.grant.reserved_cpu == broker.grant.spent_cpu == 0
    assert list(pending.budget_entries.order_by("created_at").values_list("action", flat=True)) == ["hold", "hold_release"]
    assert ApprovalDecision.objects.get().comment == "Use a smaller ensemble."
    assert not Attempt.objects.exists()


def test_pending_cancellation_releases_once_and_prevents_later_approval(broker):
    allowance(broker)
    job = prepared_job(broker)
    pending = execution(broker, job)
    first = services.cancel_execution_request(pending)
    assert services.cancel_execution_request(pending) == first
    with pytest.raises(BrokerError):
        decide(broker, job, pending)
    pending.refresh_from_db()
    broker.grant.refresh_from_db()
    assert pending.state == "cancelled" and pending.reservation_state == "released"
    assert broker.grant.reserved_cpu == 0
    assert pending.budget_entries.filter(action="hold_release").count() == 1
    assert not ApprovalDecision.objects.exists() and not Attempt.objects.exists()


def test_approval_converts_its_fully_funded_hold_without_reserving_twice(broker):
    allowance(broker)
    job = prepared_job(broker)
    pending = execution(broker, job)
    assert services.allowance(broker.project)["available"]["cpu_core_seconds"] == 0
    decide(broker, job, pending)
    pending.refresh_from_db()
    broker.grant.refresh_from_db()
    assert pending.state == "authorized" and pending.reservation_state == "converted"
    assert broker.grant.reserved_cpu == pending.attempt.reservation_cpu == 60
    assert pending.budget_entries.filter(action="hold_convert").count() == 1
    assert pending.attempt.reservation_cpu == 60
    assert ApprovalDecision.objects.get(execution_request=pending).consumed_at is not None
    snapshot = services.allowance(broker.project)
    assert snapshot["reserved_awaiting_approval"]["cpu_core_seconds"] == 0
    assert snapshot["jobs"]["awaiting_approval"] == 0 and snapshot["jobs"]["admitted"] == 1
    assert pending.attempt.ledgerentry_set.filter(action="reserve").count() == 1


@pytest.mark.parametrize("change,code", [
    ("pause", "PROJECT_PAUSED"), ("revoke", "AUTH_REQUIRED"), ("expiry", "GRANT_EXPIRED"),
    ("tighten", "RESOURCE_LIMIT"), ("debt", "BUDGET_UNAVAILABLE"),
])
def test_failed_current_policy_approval_keeps_hold_and_rolls_back_decision(broker, change, code):
    allowance(broker, 120)
    job = prepared_job(broker, cpus=2)
    pending = execution(broker, job)
    if change == "pause":
        Project.objects.filter(id=broker.project.id).update(paused=True)
    elif change == "revoke":
        ProjectCredential.objects.filter(id=broker.credential.id).update(revoked=True)
    elif change == "expiry":
        policy(broker, admission_expires_at=(timezone.now() - timedelta(seconds=1)).isoformat())
    elif change == "tighten":
        limits = {**broker.grant.policy["per_job_limits"], "cpu_cores": 1}
        policy(broker, per_job_limits=limits)
    else:
        Grant.objects.filter(id=broker.grant.id).update(spent_cpu=1)
    with pytest.raises(BrokerError) as denied:
        decide(broker, job, pending)
    assert denied.value.code == code
    pending.refresh_from_db()
    broker.grant.refresh_from_db()
    assert pending.state == "awaiting_approval" and pending.reservation_state == "held"
    assert broker.grant.reserved_cpu == 120
    assert pending.budget_entries.count() == 1
    assert not ApprovalDecision.objects.exists() and not Attempt.objects.exists()
    services.cancel_execution_request(pending)
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 0


def test_pending_gpu_hold_blocks_gpu_overspend_without_consuming_unrelated_cpu_balance(broker):
    allowance(broker, 120, 60)
    job = gpu_job(broker)
    pending = execution(broker, job)
    with pytest.raises(BrokerError) as denied:
        execution(broker, job, another_key(broker))
    assert denied.value.code == "BUDGET_UNAVAILABLE"
    cpu_pending = execution(broker, prepared_job(broker))
    assert services.allowance(broker.project)["reserved"] == {"cpu_core_seconds": 120, "gpu_device_seconds": 60}
    services.cancel_execution_request(pending)
    assert services.allowance(broker.project)["reserved"] == {"cpu_core_seconds": cpu_pending.reservation_cpu, "gpu_device_seconds": 0}


def test_mixed_envelope_review_holds_full_effective_cost_before_human_override(broker):
    allowance(broker, 120)
    policy(broker, mode="mixed", per_job_limits={**broker.grant.policy["per_job_limits"], "cpu_cores": 1})
    job = prepared_job(broker, cpus=2)
    pending = execution(broker, job)
    assert pending.reservation_cpu == 120 and pending.reservation_state == "held"
    assert any(reason["code"] == "PROJECT_ENVELOPE_REVIEW_REQUIRED" for reason in pending.approval_requirements["approval_reasons"])
    decide(broker, job, pending)
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 120 and Attempt.objects.count() == 1


def test_budgeted_execution_bypasses_pending_hold_and_reserves_one_attempt(broker):
    budgeted(broker, 60, 0)
    pending = execution(broker, prepared_job(broker))
    assert pending.state == "authorized" and pending.reservation_state == "none"
    assert not LedgerEntry.objects.filter(action="hold").exists()
    assert Attempt.objects.get().reservation_cpu == 60


@pytest.mark.parametrize("review_gate,reason", [("key", "KEY_REVIEW_REQUIRED"), ("cluster", "CLUSTER_REVIEW_REQUIRED")])
def test_independent_key_and_cluster_review_gates_hold_budget_in_budgeted_projects(broker, review_gate, reason):
    budgeted(broker, 60, 0)
    if review_gate == "key":
        broker.credential.execution_mode = "approval_required"
        broker.credential.save(update_fields=["execution_mode"])
    else:
        broker.connection.configuration = {**broker.connection.configuration, "site_profile": {"automatic_execution_allowed": False}}
        broker.connection.revision_sha256 = services.digest(broker.connection.configuration)
        broker.connection.save(update_fields=["configuration", "revision_sha256"])
    pending = execution(broker, prepared_job(broker))
    assert pending.state == "awaiting_approval" and pending.reservation_state == "held"
    assert reason in {item["code"] for item in pending.approval_requirements["approval_reasons"]}
    assert not Attempt.objects.exists()


def test_http_request_replay_and_cancellation_are_idempotent_and_job_scoped(broker):
    allowance(broker)
    job = prepared_job(broker)
    first = api_post(broker, f"/api/v1/jobs/{job.id}/execution-requests")
    second = api_post(broker, f"/api/v1/jobs/{job.id}/execution-requests")
    assert first.status_code == second.status_code == 202 and first.json() == second.json()
    pending = ExecutionRequest.objects.get()
    assert first.json()["budget_reservation"]["state"] == "held"
    assert api_post(broker, cancel_url(prepared_job(broker), pending), key="wrong-job").status_code == 404
    cancelled = api_post(broker, cancel_url(job, pending), key="cancel-pending")
    assert cancelled.status_code == 202
    assert api_post(broker, cancel_url(job, pending), key="cancel-pending").json() == cancelled.json()
    assert api_post(broker, cancel_url(job, pending), key="cancel-again").status_code == 202
    assert pending.budget_entries.filter(action="hold_release").count() == 1
    assert services.allowance(broker.project)["reserved"]["cpu_core_seconds"] == 0


def test_cancellation_api_requires_cancel_scope_and_same_project(broker):
    job = prepared_job(broker)
    pending = execution(broker, job)
    narrow = another_key(broker)
    narrow.scopes = ["jobs:read"]
    narrow.save(update_fields=["scopes"])
    assert api_post(broker, cancel_url(job, pending), token="another-pending-review-key").status_code == 403
    other_project = Project.objects.create(owner=broker.owner, name="Different project")
    other_token = "foreign-pending-cancel-key"
    ProjectCredential.objects.create(project=other_project, issued_by=broker.owner, name="foreign", prefix="foreign",
        scopes=["jobs:cancel"], token_hash=token_hash(other_token), expires_at=timezone.now() + timedelta(days=1))
    assert api_post(broker, cancel_url(job, pending), token=other_token).status_code == 404
    assert Client().post(cancel_url(job, pending), "{}", content_type="application/json").status_code == 401
    pending.refresh_from_db()
    assert pending.reservation_state == "held"


def test_another_authorized_project_key_can_release_a_revoked_requesters_hold(broker):
    allowance(broker)
    job = prepared_job(broker)
    pending = execution(broker, job)
    another_key(broker)
    ProjectCredential.objects.filter(id=broker.credential.id).update(revoked=True)
    assert api_post(broker, cancel_url(job, pending)).status_code == 401
    assert api_post(broker, cancel_url(job, pending), token="another-pending-review-key").status_code == 202
    pending.refresh_from_db()
    assert pending.reservation_state == "released"
    assert services.allowance(broker.project)["available"]["cpu_core_seconds"] == 60


def test_cancellation_requires_post_with_an_empty_document_before_releasing_a_hold(broker):
    job = prepared_job(broker)
    pending = execution(broker, job)
    path = cancel_url(job, pending)
    assert Client().get(path, HTTP_AUTHORIZATION="Bearer " + broker.token).status_code == 405
    assert api_post(broker, path, document={"budget_override": True}).status_code == 400
    pending.refresh_from_db()
    assert pending.reservation_state == "held" and not pending.budget_entries.filter(action="hold_release").exists()


def test_human_review_cancellation_requires_csrf_and_releases_budget_once(broker):
    job = prepared_job(broker)
    pending = execution(broker, job)
    human = Client(enforce_csrf_checks=True)
    human.force_login(broker.owner)
    document = {"execution_request_id": str(pending.id), "manifest_sha256": job.manifest_sha256,
        "decision": "cancel", "idempotency_key": "human-cancel-held-request"}
    assert human.post(f"/review/{job.id}/", document).status_code == 403
    assert human.get(f"/review/{job.id}/").status_code == 200
    for _ in range(2):
        assert human.post(f"/review/{job.id}/", document, HTTP_X_CSRFTOKEN=human.cookies["csrftoken"].value).status_code == 302
    pending.refresh_from_db()
    assert pending.state == "cancelled" and pending.reservation_state == "released"
    assert pending.budget_entries.filter(action="hold_release").count() == 1
    assert not ApprovalDecision.objects.exists()


@pytest.mark.parametrize("state", ["submission_uncertain", "running"])
def test_cancelling_after_dispatch_keeps_funds_until_authoritative_accounting(broker, state):
    allowance(broker)
    job = prepared_job(broker)
    pending = execution(broker, job)
    decide(broker, job, pending)
    attempt = pending.attempt
    lease = services.lease_work(broker.worker)["lease"]
    services.dispatch(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "manifest_sha256": job.manifest_sha256, "connection_revision_sha256": broker.connection.revision_sha256})
    if state == "submission_uncertain":
        services.report(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
            "outcome": "submission_uncertain"})
    else:
        _, observe = submit_receipt(broker, attempt, lease)
        services.report(broker.worker, attempt.id, {"lease_id": observe["id"], "generation": observe["generation"],
            "outcome": "observed", "observation": {"state": "running", "terminal": False}})
    services.cancel_execution_request(pending)
    services.cancel_execution_request(pending)
    attempt.refresh_from_db()
    broker.grant.refresh_from_db()
    assert attempt.state == state and attempt.cancel_requested is True
    assert attempt.accounting_state == "reserved" and broker.grant.reserved_cpu == 60
    assert not LedgerEntry.objects.filter(action__in=["release", "hold_release", "settle"]).exists()


def test_converted_hold_settles_actual_cancelled_job_usage_once(broker):
    allowance(broker)
    job = prepared_job(broker)
    pending = execution(broker, job)
    decide(broker, job, pending)
    attempt = pending.attempt
    lease = services.lease_work(broker.worker)["lease"]
    services.dispatch(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "manifest_sha256": job.manifest_sha256, "connection_revision_sha256": broker.connection.revision_sha256})
    native, running = submit_receipt(broker, attempt, lease)
    services.report(broker.worker, attempt.id, {"lease_id": running["id"], "generation": running["generation"],
        "outcome": "observed", "native_identity": native, "observation": {"state": "running", "terminal": False}})
    services.cancel_execution_request(pending)
    cancellation = services.lease_work(broker.worker)["lease"]
    assert cancellation["operation"] == "cancel"
    services.report(broker.worker, attempt.id, {"lease_id": cancellation["id"], "generation": cancellation["generation"],
        "outcome": "cancel_requested", "native_identity": native})
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 60 and broker.grant.spent_cpu == 0
    Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now() - timedelta(seconds=1))
    observation = services.lease_work(broker.worker)["lease"]
    assert observation["operation"] == "observe"
    report = {"lease_id": observation["id"], "generation": observation["generation"],
        "outcome": "observed", "native_identity": native, "observation": {"state": "cancelled", "terminal": True,
            "accounting": {"cpu_core_seconds": 2, "gpu_device_seconds": 0, "final": True}}}
    first = services.report(broker.worker, attempt.id, report)
    assert services.report(broker.worker, attempt.id, report) == first
    broker.grant.refresh_from_db()
    assert broker.grant.spent_cpu == 2 and broker.grant.reserved_cpu == 0
    assert services.allowance(broker.project)["available"]["cpu_core_seconds"] == 58
    assert LedgerEntry.objects.filter(attempt=attempt, action="settle").count() == 1
    assert not pending.budget_entries.filter(action="hold_release").exists()
