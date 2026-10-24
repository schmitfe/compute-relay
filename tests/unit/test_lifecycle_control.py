"""Scheduler management remains reachable and accounting survives lease recovery."""

import base64
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.auth import token_hash
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Attempt, Grant, LedgerEntry, Project, ProjectCredential, WorkerReport
from test_control import broker, budgeted, dispatched, execution, prepared_job  # noqa: F401


def another_project(broker):
    other = SimpleNamespace(**vars(broker))
    other.project = Project.objects.create(owner=broker.owner, name="another synthetic project")
    other.project.connections.add(broker.connection)
    other.project.environments.add(broker.environment)
    other.credential = ProjectCredential.objects.create(project=other.project, issued_by=broker.owner,
        name="another key", prefix="another", token_hash=token_hash("another-synthetic-project-token"),
        scopes=broker.credential.scopes, expires_at=broker.credential.expires_at)
    policy = deepcopy(broker.policy)
    policy.update(project_id=str(other.project.id), mode="budgeted")
    other.grant = Grant.objects.create(project=other.project, policy=policy)
    return other


@pytest.mark.parametrize("blocked", ["paused", "disabled", "unsupported"])
@pytest.mark.parametrize("operation", ["observe", "cancel"])
def test_thirty_five_ineligible_submissions_cannot_starve_other_project_management(broker, monkeypatch, blocked, operation):
    budgeted(broker)
    old = prepared_job(broker)
    for _ in range(35):
        execution(broker, old)
    other = another_project(broker)
    live = execution(other, prepared_job(other)).attempt
    Attempt.objects.filter(id=live.id).update(dispatch_started=True, state="running",
        native_identity={"job_id": "17", "attempt_id": str(live.id)}, cancel_requested=operation == "cancel")
    if blocked == "paused":
        broker.project.paused = True
        broker.project.save(update_fields=["paused"])
    elif blocked == "disabled":
        broker.connection.enabled = False
        broker.connection.save(update_fields=["enabled"])
    else:
        old.extension_name = "environments"
        old.save(update_fields=["extension_name"])
        monkeypatch.setattr("compute_relay.extensions.control_names", lambda: ("environments",))
        broker.worker.supported_extensions = []
    lease = services.lease_work(broker.worker)["lease"]
    assert lease["attempt_id"] == str(live.id)
    assert lease["operation"] == operation


def test_submission_filter_happens_before_batch_limit(broker):
    budgeted(broker)
    old = prepared_job(broker)
    for _ in range(35):
        execution(broker, old)
    other = another_project(broker)
    pending = execution(other, prepared_job(other)).attempt
    broker.project.paused = True
    broker.project.save(update_fields=["paused"])
    assert services.lease_work(broker.worker, management_only=True)["lease"] is None
    lease = services.lease_work(broker.worker)["lease"]
    assert lease["attempt_id"] == str(pending.id) and lease["operation"] == "submit"


def cancellation_lease(broker, *, outputs=None):
    budgeted(broker)
    job = prepared_job(broker, outputs=outputs)
    attempt, lease = dispatched(broker, job)
    native = {"job_id": "17", "attempt_id": str(attempt.id), "cluster": "synthetic"}
    services.report(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "outcome": "submitted", "native_identity": native})
    Attempt.objects.filter(id=attempt.id).update(cancel_requested=True, next_poll_at=timezone.now() - timedelta(seconds=1))
    return attempt, services.lease_work(broker.worker)["lease"], native


def payload(lease, outcome, native, **extra):
    return {"lease_id": lease["id"], "generation": lease["generation"], "outcome": outcome,
        "native_identity": native, **extra}


def make_due(attempt):
    Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now() - timedelta(seconds=1))


@pytest.mark.parametrize("outcome,error,next_operation", [
    ("cancel_unknown", "CANCEL_UNCONFIRMED", "cancel"),
    ("cancel_identity_mismatch", "CANCEL_IDENTITY_MISMATCH", "observe"),
])
def test_unknown_or_mismatched_cancel_keeps_intent_and_reservation(broker, outcome, error, next_operation):
    attempt, lease, native = cancellation_lease(broker)
    services.report(broker.worker, attempt.id, payload(lease, outcome, native, error_code="UNTRUSTED"))
    attempt.refresh_from_db()
    broker.grant.refresh_from_db()
    assert attempt.cancel_requested is True and attempt.accounting_state == "reserved"
    assert attempt.error_code == error
    assert broker.grant.reserved_cpu == attempt.reservation_cpu
    assert services.lease_work(broker.worker)["lease"] is None
    make_due(attempt)
    assert services.lease_work(broker.worker)["lease"]["operation"] == next_operation


def test_explicit_cancel_acceptance_does_not_release_accounting_reservation(broker):
    attempt, lease, native = cancellation_lease(broker)
    services.report(broker.worker, attempt.id, payload(lease, "cancel_requested", native))
    attempt.refresh_from_db()
    assert attempt.cancel_requested is False and attempt.state == "cancel_requested"
    assert attempt.accounting_state == "reserved"
    assert not LedgerEntry.objects.filter(attempt=attempt, action__in=["release", "settle"]).exists()
    make_due(attempt)
    assert services.lease_work(broker.worker)["lease"]["operation"] == "observe"


def test_repeated_unknown_cancel_backs_off_and_explicit_retry_resets_identity_warning(broker):
    attempt, lease, native = cancellation_lease(broker)
    services.report(broker.worker, attempt.id, payload(lease, "cancel_unknown", native))
    make_due(attempt)
    lease = services.lease_work(broker.worker)["lease"]
    before = timezone.now()
    services.report(broker.worker, attempt.id, payload(lease, "cancel_unknown", native))
    attempt.refresh_from_db()
    assert attempt.next_poll_at >= before + timedelta(seconds=29)
    make_due(attempt)
    lease = services.lease_work(broker.worker)["lease"]
    services.report(broker.worker, attempt.id, payload(lease, "cancel_identity_mismatch", native))
    services.cancel_attempt(attempt)
    attempt.refresh_from_db()
    assert attempt.cancel_requested is True and attempt.error_code == ""
    assert services.lease_work(broker.worker)["lease"]["operation"] == "cancel"


def test_verified_completion_after_failed_cancel_settles_once_even_when_storage_busy(broker, monkeypatch):
    attempt, lease, native = cancellation_lease(broker, outputs=["result.txt"])
    services.report(broker.worker, attempt.id, payload(lease, "cancel_unknown", native))
    make_due(attempt)
    lease = services.lease_work(broker.worker)["lease"]
    observation = {"state": "completed", "terminal": True,
        "accounting": {"cpu_core_seconds": 7, "gpu_device_seconds": 0, "final": True}}
    document = payload(lease, "observed", native, observation=observation,
        artifacts={"result.txt": base64.b64encode(b"result").decode()})
    def busy(*args, **kwargs):
        raise BrokerError("UPLOAD_BUSY", "A writer is active.", 409)
    monkeypatch.setattr(services, "store_blob", busy)
    first = services.report(broker.worker, attempt.id, document)
    assert services.report(broker.worker, attempt.id, document) == first
    attempt.refresh_from_db()
    broker.grant.refresh_from_db()
    assert attempt.cancel_requested is False and attempt.accounting_state == "settled"
    assert attempt.error_code == "OUTPUT_STORAGE_UNAVAILABLE"
    assert broker.grant.spent_cpu == 7 and broker.grant.reserved_cpu == 0
    assert LedgerEntry.objects.filter(attempt=attempt, action="settle").count() == 1


def test_cancellation_result_requires_exact_native_identity(broker):
    attempt, lease, native = cancellation_lease(broker)
    with pytest.raises(BrokerError) as error:
        services.report(broker.worker, attempt.id, payload(lease, "cancel_requested", {**native, "job_id": "99"}))
    assert error.value.code == "NATIVE_IDENTITY_CHANGED"
    attempt.refresh_from_db()
    assert attempt.cancel_requested is True


def test_expired_unsuperseded_lease_renews_with_fixed_deadline_and_rejects_replaced_generation(broker, monkeypatch):
    budgeted(broker)
    attempt = execution(broker, prepared_job(broker)).attempt
    start = timezone.now()
    monkeypatch.setattr(services.timezone, "now", lambda: start)
    lease = services.lease_work(broker.worker)["lease"]
    document = {"lease_id": lease["id"], "generation": lease["generation"]}
    deadline = start + timedelta(seconds=services.MAX_LEASE_LIFETIME_SECONDS)
    monkeypatch.setattr(services.timezone, "now", lambda: start + timedelta(seconds=services.LEASE_SECONDS + 1))
    assert services.renew_lease(broker.worker, attempt.id, document)["renewed"] is True
    attempt.refresh_from_db()
    assert attempt.lease_deadline_at == deadline
    monkeypatch.setattr(services.timezone, "now", lambda: deadline - timedelta(seconds=10))
    services.renew_lease(broker.worker, attempt.id, document)
    attempt.refresh_from_db()
    assert attempt.lease_expires_at == deadline and attempt.lease_deadline_at == deadline
    monkeypatch.setattr(services.timezone, "now", lambda: deadline)
    with pytest.raises(BrokerError) as expired:
        services.renew_lease(broker.worker, attempt.id, document)
    assert expired.value.code == "STALE_LEASE"
    replacement = services.lease_work(broker.worker)["lease"]
    assert replacement["id"] != lease["id"] and replacement["generation"] > lease["generation"]
    with pytest.raises(BrokerError) as stale:
        services.renew_lease(broker.worker, attempt.id, document)
    assert stale.value.code == "STALE_LEASE"
    assert services.renew_lease(broker.worker, attempt.id,
        {"lease_id": replacement["id"], "generation": replacement["generation"]})["renewed"] is True


def test_result_operation_pair_is_checked_without_releasing_budget(broker):
    attempt, lease, native = cancellation_lease(broker)
    with pytest.raises(BrokerError) as error:
        services.report(broker.worker, attempt.id, payload(lease, "rejected", native))
    assert error.value.code == "INVALID_REPORT"
    attempt.refresh_from_db()
    assert attempt.cancel_requested is True and attempt.accounting_state == "reserved"


def test_unknown_poll_receipts_are_bounded_and_pruned_generations_cannot_replay(broker):
    attempt, lease, native = cancellation_lease(broker)
    observation = {"state": "unknown", "terminal": False}
    oldest = payload(lease, "cancel_unknown", native, observation=observation)
    services.report(broker.worker, attempt.id, oldest)
    for _ in range(services.MAX_REPORT_RECEIPTS_PER_ATTEMPT + 4):
        make_due(attempt)
        lease = services.lease_work(broker.worker)["lease"]
        current = payload(lease, "cancel_unknown", native, observation=observation)
        response = services.report(broker.worker, attempt.id, current)
        assert WorkerReport.objects.filter(attempt=attempt).count() <= services.MAX_REPORT_RECEIPTS_PER_ATTEMPT
    assert services.report(broker.worker, attempt.id, current) == response
    assert not WorkerReport.objects.filter(lease_id=oldest["lease_id"]).exists()
    with pytest.raises(BrokerError) as stale:
        services.report(broker.worker, attempt.id, oldest)
    assert stale.value.code == "STALE_LEASE"
    attempt.refresh_from_db()
    assert attempt.cancel_requested is True and attempt.accounting_state == "reserved"
    make_due(attempt)
    lease = services.lease_work(broker.worker)["lease"]
    final = payload(lease, "observed", native, observation={"state": "cancelled", "terminal": True,
        "accounting": {"cpu_core_seconds": 3, "gpu_device_seconds": 0, "final": True}})
    settled = services.report(broker.worker, attempt.id, final)
    assert services.report(broker.worker, attempt.id, final) == settled
    assert WorkerReport.objects.filter(attempt=attempt).count() == services.MAX_REPORT_RECEIPTS_PER_ATTEMPT
    assert LedgerEntry.objects.filter(attempt=attempt, action="settle").count() == 1


def test_slow_unconfirmed_cancels_cannot_starve_overdue_observation(broker, monkeypatch):
    budgeted(broker)
    job = prepared_job(broker)
    attempts = [execution(broker, job).attempt for _ in range(3)]
    initial = timezone.now()
    clock = [initial]
    identities = {}
    for index, attempt in enumerate(attempts):
        identities[str(attempt.id)] = {"job_id": str(17 + index), "attempt_id": str(attempt.id)}
        Attempt.objects.filter(id=attempt.id).update(dispatch_started=True, state="running",
            native_identity=identities[str(attempt.id)], cancel_requested=index < 2,
            next_poll_at=initial - timedelta(seconds=1))
    monkeypatch.setattr(services.timezone, "now", lambda: clock[0])
    selected = []
    for _ in range(10):
        lease = services.lease_work(broker.worker, management_only=True)["lease"]
        selected.append(lease["attempt_id"])
        if lease["operation"] == "observe":
            assert lease["attempt_id"] == str(attempts[2].id)
            break
        assert lease["operation"] == "cancel"
        clock[0] += timedelta(seconds=32)
        services.report(broker.worker, lease["attempt_id"], payload(lease, "cancel_unknown", identities[lease["attempt_id"]]))
        clock[0] += timedelta(seconds=1)
    assert selected == [str(attempts[0].id), str(attempts[1].id), str(attempts[2].id)]


def test_management_due_age_precedes_kind_and_management_precedes_old_submission(broker):
    budgeted(broker)
    job = prepared_job(broker)
    pending, cancelling, observing = [execution(broker, job).attempt for _ in range(3)]
    now = timezone.now()
    Attempt.objects.filter(id=pending.id).update(next_poll_at=now - timedelta(hours=1))
    for attempt in (cancelling, observing):
        Attempt.objects.filter(id=attempt.id).update(dispatch_started=True, state="running",
            native_identity={"job_id": str(attempt.id), "attempt_id": str(attempt.id)},
            cancel_requested=attempt == cancelling, next_poll_at=now - timedelta(seconds=1))
    # Cancellation retains preference when management work has the same due age.
    assert services.lease_work(broker.worker)["lease"]["attempt_id"] == str(cancelling.id)
    assert services.lease_work(broker.worker)["lease"]["attempt_id"] == str(observing.id)
    assert services.lease_work(broker.worker)["lease"]["attempt_id"] == str(pending.id)
