"""Worker failure context is useful without exposing private exception content."""

import asyncio
from copy import deepcopy
import logging
import time
from uuid import uuid4

import pytest

from compute_relay import diagnostics
from compute_relay.backends import SlurmBackend
from compute_relay.transports import TransportError
from compute_relay.worker.client import ControlError
from test_support.unit_test_async_worker import configured


def failures(caplog):
    return [record for record in caplog.records if record.name == "compute_relay.diagnostics"]


def check_context(record, lease):
    assert record.operation == lease["operation"]
    assert record.attempt_id == lease["attempt_id"]
    assert record.connection_id == lease["connection_id"]
    assert record.generation == lease["generation"]
    assert type(record.duration_ms) is int and record.duration_ms >= 0
    assert record.exc_info is None and record.stack_info is None


@pytest.mark.parametrize("phase,method,operation", [
    ("submit", "submit", "submit"), ("logs", "read_logs", "observe"),
    ("collect", "output_manifest", "observe"), ("cancel", "cancel", "cancel"),
    ("cleanup", "cleanup", "cleanup"), ("observe", "observe", "observe"),
    ("execute", "reconcile", "reconcile"), ("availability", "availability", "availability"),
])
def test_worker_logs_failure_phase_and_preserves_recovery(configured, caplog, monkeypatch, phase, method, operation):
    c = configured
    c.lease.update(operation=operation, native_identity={"job_id": "17", "attempt_id": c.lease["attempt_id"]},
        connection_revision_sha256=c.lease["prepared"]["manifest"]["connection_revision_sha256"])
    async def fail(*args, **kwargs):
        raise OSError("PRIVATE_TOKEN /private/path remote stderr\nforged-log")
    monkeypatch.setattr(c.backend, method, fail, raising=False)
    if method in {"observe", "reconcile"}:
        with pytest.raises(OSError):
            asyncio.run(c.worker.execute(c.lease))
    else:
        asyncio.run(c.worker.execute(c.lease))
    records = [record for record in failures(caplog) if record.phase == phase]
    assert len(records) == 1
    check_context(records[0], c.lease)
    assert records[0].category == "io" and records[0].error_type == "OSError"
    assert "PRIVATE_TOKEN" not in caplog.text and "private/path" not in caplog.text and "forged-log" not in caplog.text
    if method == "submit":
        assert c.journal.read(c.lease["attempt_id"])["outcome"] == "submission_uncertain"
        assert c.control.calls[-1][2]["outcome"] == "submission_uncertain"
    elif method in {"read_logs", "output_manifest"}:
        assert c.control.calls[-1][2]["observation"]["accounting"]["final"] is True


def test_report_failure_keeps_receipt_and_safe_context(configured, caplog):
    c = configured
    c.control.fail_report = True
    with pytest.raises(ControlError):
        asyncio.run(c.worker.execute(c.lease))
    record = next(record for record in failures(caplog) if record.phase == "report")
    check_context(record, c.lease)
    assert record.category == "control"
    assert c.journal.read_report(c.lease["attempt_id"])["phase"] == "ready"
    asyncio.run(c.worker.execute(c.lease))
    assert c.backend.submitted == 1 and c.journal.read_report(c.lease["attempt_id"]) is None


def test_backend_caught_transport_error_keeps_worker_context_and_uncertainty(configured, caplog):
    c = configured
    class Unavailable:
        async def call_helper(self, *args):
            raise TransportError("PRIVATE_TOKEN remote path and stderr")
    # Exercise the real backend catch under the worker's diagnostic context.
    backend = SlurmBackend({}, transport=Unavailable())
    backend._verify_binding = lambda manifest: None
    async def scenario():
        with diagnostics.operation(c.lease, "execute"):
            return await backend.reconcile(c.lease["attempt_id"], c.lease["prepared"]["manifest"])
    assert asyncio.run(scenario())["outcome"] == "submission_uncertain"
    record, = failures(caplog)
    check_context(record, c.lease)
    assert record.phase == "remote" and record.category == "transport"
    assert "PRIVATE_TOKEN" not in caplog.text


def test_parallel_attempt_failures_keep_their_own_context(configured, caplog, monkeypatch):
    c = configured
    leases = [{**deepcopy(c.lease), "id": str(uuid4()), "attempt_id": str(uuid4()), "operation": "observe"}
        for _ in range(4)]
    for lease in leases:
        lease["native_identity"] = {"job_id": "17", "attempt_id": lease["attempt_id"]}
    async def fail(*args, **kwargs):
        await asyncio.sleep(0)
        raise OSError("PRIVATE_TOKEN")
    monkeypatch.setattr(c.backend, "output_manifest", fail)
    async def scenario():
        await asyncio.gather(*(c.worker.execute(lease) for lease in leases))
    asyncio.run(scenario())
    records = {record.attempt_id: record for record in failures(caplog) if record.phase == "collect"}
    assert set(records) == {lease["attempt_id"] for lease in leases}
    for lease in leases:
        check_context(records[lease["attempt_id"]], lease)
    # Nested and completed operations must not leak context into later work.
    diagnostics.failure(None, "poll", RuntimeError("PRIVATE_TOKEN"), time.monotonic())
    assert failures(caplog)[-1].attempt_id is None and "PRIVATE_TOKEN" not in caplog.text


def test_hostile_metadata_and_exception_class_cannot_add_log_content(caplog):
    caplog.set_level(logging.WARNING)
    hostile_metadata = "PRIVATE_TOKEN\nforged-log"
    error = type(hostile_metadata, (Exception,), {})(hostile_metadata)
    with pytest.raises(Exception) as caught:
        with diagnostics.operation({"operation": {hostile_metadata: hostile_metadata}, "attempt_id": hostile_metadata,
                "connection_id": hostile_metadata, "generation": hostile_metadata}, hostile_metadata):
            raise error
    assert caught.value is error
    record, = failures(caplog)
    assert record.operation == record.phase == "unknown"
    assert record.attempt_id is record.connection_id is record.generation is None
    assert record.error_type == "Exception" and record.category == "unexpected"
    assert "PRIVATE_TOKEN" not in caplog.text and "forged-log" not in caplog.text


def test_cancellation_is_not_logged_as_an_operation_failure(caplog):
    with pytest.raises(asyncio.CancelledError):
        with diagnostics.operation(None, "poll"):
            raise asyncio.CancelledError()
    assert failures(caplog) == []
