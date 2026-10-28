"""One lifecycle contract exercised by the async execution path, without SSH."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from compute_relay.domain.lifecycle import replay_result, report_record, validate_report_outcome
from compute_relay.worker.runner import Worker
from compute_relay.worker.client import ControlError
from compute_relay.worker.journal import Journal
from test_support.unit_test_async_worker import configured


class InterruptedCollection(BaseException):
    """A process interruption must bypass ordinary output-error recovery."""


class BackendState:
    def __init__(self):
        self.calls = []
        self.cancel_reply = {"outcome": "unknown"}
        self.cancel_error = False
        self.observation = {"state": "running", "terminal": False}
        self.interrupt_collection = False
        self.delay = 0

    def submit(self, attempt_id, manifest, files):
        self.calls.append("submit")
        return {"outcome": "submitted", "native_identity": {"job_id": "17", "attempt_id": attempt_id}}

    def reconcile(self, attempt_id, manifest):
        self.calls.append("reconcile")
        return {"outcome": "submission_uncertain"}

    def cancel(self, native, *, manifest):
        self.calls.append("cancel")
        if self.cancel_error:
            raise OSError("private transport diagnostic")
        return deepcopy(self.cancel_reply)

    def observe(self, native, *, manifest):
        self.calls.append("observe")
        return deepcopy(self.observation)

    def collect(self, native, outputs, *, manifest):
        self.calls.append("collect")
        if self.interrupt_collection:
            raise InterruptedCollection()
        return {"stdout": "result", "stderr": "", "artifacts": {}}


class ControlState:
    def __init__(self):
        self.calls = []
        self.failed_reports = 0
        self.renewal_fails = False

    def request(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        if path.endswith("/dispatch"):
            return {"authorized": True}
        if path.endswith("/renew"):
            if self.renewal_fails:
                raise ControlError("STALE_LEASE", status=409)
            return {"renewed": True}
        if path.endswith("/report") and self.failed_reports:
            self.failed_reports -= 1
            raise ControlError("CONTROL_UNAVAILABLE")
        if path == "/internal/v1/lease":
            return {"lease": None}
        return {"accepted": True}




class AsyncBackend:
    def __init__(self, state):
        self.state = state

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            if name == "collect":
                await asyncio.sleep(self.state.delay)
            return getattr(self.state, name)(*args, **kwargs)
        return call


class AsyncControl:
    def __init__(self, state):
        self.state = state

    async def request(self, *args, **kwargs):
        await asyncio.sleep(0)
        return self.state.request(*args, **kwargs)


@pytest.fixture
def lifecycle(configured):
    c = configured
    backend, control = BackendState(), ControlState()

    def worker():
        common = {"backend_factory": lambda _: AsyncBackend(backend), "extensions": []}
        journal = Journal(c.journal.root)
        return Worker(AsyncControl(control), c.worker.connections, journal, owner_id=uuid4(), **common)

    return SimpleNamespace(worker=worker(), restart=worker, lease=c.lease, backend=backend, control=control,
                           journal=c.journal)


def invoke(worker, method, *args):
    result = getattr(worker, method)(*args)
    async def run():
        return await asyncio.wait_for(result, timeout=2)
    return asyncio.run(run())


def management(c, operation="cancel"):
    c.lease.update(operation=operation, native_identity={"job_id": "17", "attempt_id": c.lease["attempt_id"]})


def final_observation(c):
    c.backend.observation = {"state": "completed", "terminal": True,
        "accounting": {"cpu_core_seconds": 7, "gpu_device_seconds": 0, "final": True}}


def reports(c):
    return [body for _, path, body in c.control.calls if path.endswith("/report")]


@pytest.mark.parametrize("reply,expected,observes", [
    ({"outcome": "cancel_requested"}, "cancel_requested", False),
    ({"outcome": "unknown"}, "cancel_unknown", True),
    ({"outcome": "already_terminal"}, "cancel_unknown", True),
    ({"outcome": "identity_mismatch"}, "cancel_identity_mismatch", False),
    ({"returncode": 1}, "cancel_unknown", True),
    (None, "cancel_unknown", True),
])
def test_cancellation_requires_explicit_acceptance_or_verified_terminal(lifecycle, reply, expected, observes):
    c = lifecycle
    management(c)
    c.backend.cancel_reply = reply
    invoke(c.worker, "execute", c.lease)
    report = reports(c)[-1]
    assert report["outcome"] == expected
    assert report["native_identity"] == c.lease["native_identity"]
    assert ("observe" in c.backend.calls) == observes
    assert c.journal.read_report(c.lease["attempt_id"]) is None


def test_failed_cancel_transport_preserves_final_accounting_and_redacts_error(lifecycle):
    c = lifecycle
    management(c)
    c.backend.cancel_error = True
    final_observation(c)
    invoke(c.worker, "execute", c.lease)
    report = reports(c)[-1]
    assert report["outcome"] == "observed"
    assert report["observation"]["accounting"]["cpu_core_seconds"] == 7
    assert "private" not in str(report)


def test_lost_final_response_replays_original_receipt_before_acquiring_work(lifecycle):
    c = lifecycle
    management(c, "observe")
    final_observation(c)
    c.control.failed_reports = 1
    with pytest.raises(ControlError):
        invoke(c.worker, "execute", c.lease)
    assert c.journal.read_report(c.lease["attempt_id"])["phase"] == "ready"
    original = deepcopy(reports(c)[0])
    c.control.calls.clear()
    invoke(c.restart(), "poll_once")
    assert c.control.calls[0][1].endswith("/report")
    assert c.control.calls[0][2] == original
    assert c.backend.calls == ["observe", "collect"]
    assert c.journal.read_report(c.lease["attempt_id"]) is None


def test_superseded_lease_rebinds_saved_terminal_result_without_scheduler_requery(lifecycle):
    c = lifecycle
    management(c, "observe")
    final_observation(c)
    c.control.failed_reports = 1
    with pytest.raises(ControlError):
        invoke(c.worker, "execute", c.lease)
    c.lease.update(id=str(uuid4()), generation=2)
    c.backend.observation = {"state": "unknown", "terminal": False}
    invoke(c.restart(), "execute", c.lease)
    assert reports(c)[-1]["generation"] == 2
    assert reports(c)[-1]["observation"]["accounting"]["final"] is True
    assert c.backend.calls == ["observe", "collect"]


def test_saved_accounting_survives_interrupted_collection_and_new_generation(lifecycle):
    c = lifecycle
    management(c, "observe")
    final_observation(c)
    c.backend.interrupt_collection = True
    with pytest.raises(InterruptedCollection):
        invoke(c.worker, "execute", c.lease)
    assert c.journal.read_report(c.lease["attempt_id"])["phase"] == "observed"
    assert reports(c) == []
    c.backend.interrupt_collection = False
    c.backend.observation = {"state": "unknown", "terminal": False}
    c.lease.update(id=str(uuid4()), generation=2)
    invoke(c.restart(), "execute", c.lease)
    assert reports(c)[-1]["observation"]["accounting"]["final"] is True
    assert c.backend.calls == ["observe", "collect", "collect"]


def test_durable_final_report_cannot_rebind_to_different_native_identity(lifecycle):
    c = lifecycle
    management(c, "observe")
    final_observation(c)
    c.control.failed_reports = 1
    with pytest.raises(ControlError):
        invoke(c.worker, "execute", c.lease)
    c.lease.update(id=str(uuid4()), generation=2, native_identity={"job_id": "99", "attempt_id": c.lease["attempt_id"]})
    with pytest.raises(ValueError, match="Native identity changed"):
        invoke(c.restart(), "execute", c.lease)
    assert c.backend.calls == ["observe", "collect"]
    assert c.journal.read_report(c.lease["attempt_id"]) is not None


def test_prior_submission_intent_always_reconciles_after_restart(lifecycle):
    c = lifecycle
    c.journal.write(c.lease["attempt_id"], {"manifest_sha256": c.lease["prepared"]["manifest_sha256"], "outcome": "intent"})
    invoke(c.worker, "execute", c.lease)
    assert c.backend.calls == ["reconcile"]
    assert not any(path.endswith("/dispatch") for _, path, _ in c.control.calls)
    assert reports(c)[-1]["outcome"] == "submission_uncertain"


def test_slow_collection_renews_exact_generation_without_losing_accounting(lifecycle, monkeypatch):
    monkeypatch.setattr("compute_relay.worker.lease.RENEWAL_INTERVAL_SECONDS", 0.005)
    c = lifecycle
    management(c, "observe")
    final_observation(c)
    c.backend.delay = 0.04
    invoke(c.worker, "execute", c.lease)
    renewals = [body for _, path, body in c.control.calls if path.endswith("/renew")]
    assert renewals
    assert all(body == {"lease_id": c.lease["id"], "generation": c.lease["generation"]} for body in renewals)
    assert reports(c)[-1]["observation"]["accounting"]["final"] is True


def test_failed_renewal_preserves_terminal_evidence_for_next_valid_lease(lifecycle, monkeypatch):
    monkeypatch.setattr("compute_relay.worker.lease.RENEWAL_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr("compute_relay.worker.lease.RENEWAL_RETRY_SECONDS", 0.005)
    monkeypatch.setattr("compute_relay.worker.lease.RENEWAL_SAFETY_SECONDS", 0.005)
    c = lifecycle
    management(c, "observe")
    final_observation(c)
    c.backend.delay = 0.04
    from datetime import datetime, timedelta, timezone
    c.lease["lease_expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=.02)).isoformat()
    c.control.renewal_fails = True
    with pytest.raises(ControlError, match="LEASE_RENEWAL_FAILED"):
        invoke(c.worker, "execute", c.lease)
    assert reports(c) == []
    assert c.journal.read_report(c.lease["attempt_id"])["report"]["observation"]["accounting"]["final"] is True


def test_changed_extension_context_cannot_rebind_extension_metadata_but_keeps_accounting(configured):
    lease = configured.lease
    management(configured, "observe")
    lease["extension_context"] = {"environments": {"generation": 1, "lease_id": "old-authority"}}
    result = {"outcome": "observed", "native_identity": lease["native_identity"],
        "observation": {"state": "completed", "terminal": True, "accounting": {"final": True}},
        "extension_data": {"environments": {"generation": 1, "state": "ready"}}}
    saved = report_record(lease, "a" * 64, result)
    current = {**lease, "id": str(uuid4()), "generation": 2,
        "extension_context": {"environments": {"generation": 2, "lease_id": "new-authority"}}}
    replayed = replay_result(saved, current, "a" * 64, lease["native_identity"])
    assert replayed["observation"]["accounting"]["final"] is True
    assert replayed["extension_data"] == {}


@pytest.mark.parametrize("operation,outcome", [("observe", "submitted"), ("cancel", "rejected"), ("observe", {}), ([], "observed")])
def test_shared_result_validator_rejects_invalid_operation_outcome_pairs(operation, outcome):
    with pytest.raises(ValueError):
        validate_report_outcome(operation, outcome)


def test_maximum_collected_outputs_and_escaped_logs_fit_durable_report(configured):
    c = configured
    management(c, "observe")
    manifest = deepcopy(c.lease["prepared"]["manifest"])
    manifest["request"]["outputs"] = ["result.bin"]
    collected = Worker._serialized_collection({"stdout": "\0" * 65536, "stderr": "\0" * 65536,
        "artifacts": {"result.bin": b"x" * (10 * 1024 * 1024)}}, manifest)
    result = {"outcome": "observed", "native_identity": c.lease["native_identity"],
        "observation": {"state": "completed", "terminal": True},
        "extension_data": {"environments": {"diagnostic": "x" * 65000}}, **collected}
    record = report_record(c.lease, c.lease["prepared"]["manifest_sha256"], result)
    c.journal.write_report(c.lease["attempt_id"], record)
    assert c.journal.read_report(c.lease["attempt_id"]) == record
