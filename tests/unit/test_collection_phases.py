"""Terminal evidence and output recovery use separate, generation-fenced leases."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.models import Attempt, LedgerEntry, WorkerReport
from compute_relay.control.progress import log_window
from compute_relay.worker.client import ControlError
from compute_relay.worker.journal import Journal
from compute_relay.worker.runner import Worker
from test_support.unit_test_async_worker import Backend
from test_support.unit_test_async_worker import configured
from test_support.unit_test_control import broker, budgeted, dispatched, execution, prepared_job, submit_receipt


def terminal(*, final=True):
    return {"state": "completed", "terminal": True, "accounting": {
        "final": final, **({"cpu_core_seconds": 7, "gpu_device_seconds": 0} if final else {})}}


def document(lease, observation, **extra):
    return {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "observed",
        "native_identity": lease["native_identity"], "observation": observation, **extra}


def due(attempt):
    Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now() - timedelta(seconds=1))


def test_worker_publishes_terminal_evidence_before_logs_extensions_or_outputs(configured, monkeypatch):
    from compute_relay import extensions
    c = configured
    c.lease.update(operation="observe", native_identity={"job_id": "17", "attempt_id": c.lease["attempt_id"]})
    events = []
    original_logs, original_outputs = c.backend.read_logs, c.backend.output_manifest

    async def logs(*args, **kwargs):
        events.append("logs")
        return await original_logs(*args, **kwargs)

    async def finalize(*args, **kwargs):
        events.append("finalize")
        return {}

    async def outputs(*args, **kwargs):
        events.append("outputs")
        return await original_outputs(*args, **kwargs)

    monkeypatch.setattr(c.backend, "read_logs", logs)
    monkeypatch.setattr(c.backend, "output_manifest", outputs)
    monkeypatch.setattr(extensions, "worker_after_observation", finalize)
    asyncio.run(c.worker.execute(c.lease))
    core = deepcopy(c.control.calls[-1][2])
    assert core["observation"]["accounting"]["final"] is True
    assert "collection_state" not in core and "log_chunks" not in core
    assert events == []
    c.lease.update(id=str(uuid4()), generation=2, collection_only=True, observation=core["observation"])

    async def forbidden(*args, **kwargs):
        pytest.fail("Collection queried native scheduler state")

    monkeypatch.setattr(c.backend, "observe", forbidden)
    asyncio.run(c.worker.execute(c.lease))
    collected = c.control.calls[-1][2]
    assert collected["observation"] == core["observation"] and collected["collection_state"] == "complete"
    assert events == ["logs", "finalize", "outputs"] and collected["log_chunks"]


def test_all_terminal_batch_evidence_is_ready_before_first_report_can_fail(configured):
    c = configured
    leases = [{**deepcopy(c.lease), "operation": "observe", "id": str(uuid4()), "attempt_id": str(uuid4())}
        for _ in range(3)]
    for index, lease in enumerate(leases):
        lease["native_identity"] = {"job_id": str(index + 1), "attempt_id": lease["attempt_id"]}
    c.control.fail_report = True
    with pytest.raises(ControlError):
        asyncio.run(c.worker._execute_attempts(leases))
    for lease in leases:
        saved = c.journal.read_report(lease["attempt_id"])
        assert saved is not None and saved["phase"] == "ready"
        assert saved["report"]["observation"]["accounting"]["final"] is True
        assert "collection_state" not in saved["report"]


def test_provisional_terminal_collects_then_resumes_accounting_without_reopening_outputs(broker):
    budgeted(broker)
    attempt, first = dispatched(broker, prepared_job(broker, outputs=["result.txt"]))
    native, lease = submit_receipt(broker, attempt, first)
    core = document(lease, terminal(final=False))
    before = timezone.now()
    response = services.report(broker.worker, attempt.id, core)
    assert services.report(broker.worker, attempt.id, core) == response
    attempt.refresh_from_db()
    broker.grant.refresh_from_db()
    assert attempt.state == "succeeded" and attempt.accounting_state == "settlement_pending"
    assert attempt.collection_pending and attempt.collection_state == "pending"
    assert before <= attempt.next_poll_at <= timezone.now()
    assert broker.grant.reserved_cpu == attempt.reservation_cpu and broker.grant.spent_cpu == 0
    due(attempt)
    collection = services.lease_work(broker.worker)["lease"]
    assert collection["collection_only"] and collection["observation"] == core["observation"]
    assert collection["id"] != lease["id"]
    services.report(broker.worker, attempt.id, document(collection, collection["observation"], collection_state="complete"))
    attempt.refresh_from_db()
    broker.grant.refresh_from_db()
    assert not attempt.collection_pending and attempt.collection_state == "complete"
    assert broker.grant.reserved_cpu == attempt.reservation_cpu and broker.grant.spent_cpu == 0
    due(attempt)
    accounting = services.lease_work(broker.worker)["lease"]
    assert accounting["operation"] == "observe" and not accounting.get("collection_only")
    final = document(accounting, terminal())
    response = services.report(broker.worker, attempt.id, final)
    assert services.report(broker.worker, attempt.id, final) == response
    attempt.refresh_from_db()
    broker.grant.refresh_from_db()
    assert attempt.accounting_state == "settled" and not attempt.collection_pending
    assert attempt.collection_state == "complete" and broker.grant.reserved_cpu == 0 and broker.grant.spent_cpu == 7
    assert LedgerEntry.objects.filter(attempt=attempt, action="settle").count() == 1


@pytest.mark.django_db(transaction=True)
def test_lost_accepted_core_reply_replays_before_fresh_collection(broker, tmp_path, monkeypatch):
    monkeypatch.setenv("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
    budgeted(broker)
    attempt, first = dispatched(broker, prepared_job(broker))
    _, lease = submit_receipt(broker, attempt, first)
    configuration = {**broker.connection.configuration, "backend": "fake", "revision_sha256": broker.connection.revision_sha256}
    calls, observed = [], []

    class Client:
        lost = False

        async def request(self, method, path, body):
            calls.append((path, deepcopy(body)))
            if path.endswith("/report"):
                response = services.report(broker.worker, attempt.id, body)
                if not self.lost:
                    self.lost = True
                    raise ControlError("CONTROL_UNAVAILABLE")
                return response
            if path.endswith("/profiles"):
                return {}
            if path == "/internal/v1/lease":
                return services.lease_work(broker.worker, exclude_attempt_ids=body["in_progress"])
            raise AssertionError(path)

    class ObservedBackend(Backend):
        async def observe(self, items):
            observed.extend(item["native_identity"] for item in items)
            return await super().observe(items)

    client, backend = Client(), ObservedBackend()

    def restart():
        return Worker(client, {str(broker.connection.id): configuration}, Journal(tmp_path / "journal"),
            backend_factory=lambda _: backend, extensions=[], owner_id=uuid4())

    worker = restart()
    with pytest.raises(ControlError):
        asyncio.run(worker.execute(lease))
    attempt.refresh_from_db()
    broker.grant.refresh_from_db()
    assert attempt.state == "succeeded" and attempt.collection_pending and attempt.accounting_state == "settled"
    assert broker.grant.spent_cpu == 2 and broker.grant.reserved_cpu == 0
    assert not worker.journal.read_report(str(attempt.id))["report"].get("collection_state")
    calls.clear()
    assert asyncio.run(restart().poll_once()) is True
    assert calls[0][0].endswith("/report") and calls[0][1]["lease_id"] == lease["id"]
    attempt.refresh_from_db()
    broker.grant.refresh_from_db()
    assert attempt.collection_state == "complete" and not attempt.collection_pending
    assert len(observed) == 1 and backend.submitted == 0
    assert WorkerReport.objects.filter(attempt=attempt).count() == 3  # Submission, core evidence, collection.
    assert LedgerEntry.objects.filter(attempt=attempt, action="settle").count() == 1
    assert broker.grant.spent_cpu == 2 and broker.grant.reserved_cpu == 0


def test_provisional_collection_failure_retries_back_off_then_resume_accounting(broker):
    budgeted(broker)
    attempt, first = dispatched(broker, prepared_job(broker))
    _, lease = submit_receipt(broker, attempt, first)
    services.report(broker.worker, attempt.id, document(lease, terminal(final=False)))
    for number in range(1, 4):
        due(attempt)
        lease = services.lease_work(broker.worker)["lease"]
        assert lease["collection_only"]
        before = timezone.now()
        services.report(broker.worker, attempt.id, document(lease, lease["observation"],
            collection_state="failed", error_code="OUTPUT_COLLECTION_FAILED"))
        attempt.refresh_from_db()
        assert attempt.collection_failures == number and attempt.collection_pending == (number < 3)
        if number < 3:
            assert attempt.next_poll_at >= before + timedelta(seconds=15 * number)
    due(attempt)
    lease = services.lease_work(broker.worker)["lease"]
    assert not lease.get("collection_only")
    services.report(broker.worker, attempt.id, document(lease, terminal()))
    attempt.refresh_from_db()
    assert attempt.accounting_state == "settled" and attempt.collection_state == "failed"
    assert not attempt.collection_pending and attempt.collection_failures == 3


@pytest.mark.parametrize("state,pending,failures", [
    ("complete", False, 0), ("cleaned", False, 0), ("failed", False, 3), ("retry_pending", True, 1)])
def test_later_terminal_accounting_preserves_collection_decision(broker, state, pending, failures):
    budgeted(broker)
    attempt, first = dispatched(broker, prepared_job(broker))
    _, lease = submit_receipt(broker, attempt, first)
    Attempt.objects.filter(id=attempt.id).update(collection_state=state,
        collection_pending=pending, collection_failures=failures)
    services.report(broker.worker, attempt.id, document(lease, terminal()))
    attempt.refresh_from_db()
    assert (attempt.collection_state, attempt.collection_pending, attempt.collection_failures) == (state, pending, failures)


@pytest.mark.parametrize("final", [False, True])
def test_status_batches_stop_before_singleton_collection_without_skipping_age(broker, final):
    budgeted(broker)
    attempts = [execution(broker, prepared_job(broker)).attempt for _ in range(3)]
    now = timezone.now()
    for index, attempt in enumerate(attempts):
        Attempt.objects.filter(id=attempt.id).update(state="running", dispatch_started=True,
            native_identity={"job_id": str(index + 1), "attempt_id": str(attempt.id)},
            next_poll_at=now - timedelta(seconds=3 - index))
    Attempt.objects.filter(id=attempts[1].id).update(state="succeeded", collection_pending=True,
        accounting_state="settled" if final else "settlement_pending", observation=terminal(final=final))
    first = services.lease_work(broker.worker)
    assert first["lease"]["attempt_id"] == str(attempts[0].id) and "observations" not in first
    second = services.lease_work(broker.worker)
    assert second["lease"]["attempt_id"] == str(attempts[1].id)
    assert second["lease"]["collection_only"] and "observations" not in second
    assert services.lease_work(broker.worker)["lease"]["attempt_id"] == str(attempts[2].id)


def test_terminal_log_follow_waits_for_collection_tail(broker):
    budgeted(broker)
    attempt = execution(broker, prepared_job(broker)).attempt
    attempt.state, attempt.collection_pending = "succeeded", True
    assert not log_window(attempt, "stdout", 0, 65536)["finished"]
    attempt.stdout, attempt.collection_pending = "last line\n", False
    packet = log_window(attempt, "stdout", 0, 65536)
    assert packet["finished"] and packet["eof"] and packet["text"] == "last line\n"
