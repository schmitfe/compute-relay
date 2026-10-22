"""Async owner lanes and durable submission fencing without remote execution."""

import asyncio
import base64
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from compute_broker.backends import prepare
from compute_broker.worker.async_runner import AsyncWorker
from compute_broker.worker.client import ControlError
from compute_broker.worker.concurrency import PrincipalScheduler, WorkerCapacityError
from compute_broker.worker.journal import Journal


class Control:
    def __init__(self):
        self.calls, self.fail_report, self.lease = [], False, None

    async def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        await asyncio.sleep(0)
        if path.endswith("/dispatch"):
            return {"authorized": True}
        if path.endswith("/report") and self.fail_report:
            self.fail_report = False
            raise ControlError("CONTROL_UNAVAILABLE")
        if path == "/internal/v1/lease":
            return {"lease": self.lease}
        return {}


class Backend:
    def __init__(self):
        self.submitted, self.reconciled, self.cancelled = 0, 0, 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.output_failure = False

    async def submit(self, attempt_id, manifest, files):
        self.submitted += 1
        self.entered.set()
        await self.release.wait()
        return {"outcome": "submitted", "native_identity": {"job_id": "17", "attempt_id": attempt_id}}

    async def reconcile(self, attempt_id, manifest):
        self.reconciled += 1
        return {"outcome": "submission_uncertain"}

    async def observe(self, native):
        return {"state": "completed", "terminal": True, "accounting": {"cpu_core_seconds": 2, "gpu_device_seconds": 0, "final": True}}

    async def cancel(self, native):
        self.cancelled += 1
        return {"outcome": "cancel_requested"}

    async def collect(self, native, outputs):
        if self.output_failure:
            raise OSError("private remote diagnostic")
        return {"stdout": "synthetic result", "stderr": "", "artifacts": {}}


@pytest.fixture
def configured(tmp_path):
    control, backend = Control(), Backend()
    connection_id, owner_id = str(uuid4()), str(uuid4())
    connection = {"backend": "fake", "revision_sha256": "c" * 64}
    journal = Journal(tmp_path / "journal")
    worker = AsyncWorker(control, {connection_id: connection}, journal, owner_id=owner_id,
                         backend_factory=lambda _: backend, extensions=[])
    source = b"raise RuntimeError('must never execute in a connector')\n"
    request = {"schema_version": "1.0", "project_id": str(uuid4()), "connection_id": connection_id, "name": "async-test",
        "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
        "environment": {"id": str(uuid4()), "revision_sha256": "b" * 64},
        "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
        "slurm": {"partition": "debug", "account": "broker"}, "outputs": [],
        "files": [{"path": "main.py", "sha256": hashlib.sha256(source).hexdigest(), "size_bytes": len(source), "executable": False}]}
    package = prepare(request, {**request["environment"], "interpreter": "/usr/bin/python3"}, connection, str(uuid4()))
    files = {"main.py": source, **{path: text.encode() for path, text in package["generated_files"].items()}}
    lease = {"id": str(uuid4()), "operation": "submit", "attempt_id": str(uuid4()), "generation": 1,
        "connection_id": connection_id, "native_identity": None, "prepared": {"manifest": package["manifest"],
        "manifest_sha256": package["manifest_sha256"], "files": {path: base64.b64encode(data).decode() for path, data in files.items()}}}
    return SimpleNamespace(worker=worker, lease=lease, control=control, backend=backend, journal=journal)


def test_cancelled_submission_preserves_intent_and_only_reconciles_on_resume(configured):
    async def scenario():
        c = configured
        c.backend.release.clear()
        task = asyncio.create_task(c.worker.execute(c.lease))
        await c.backend.entered.wait()
        assert c.journal.read(c.lease["attempt_id"])["outcome"] == "intent"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert c.journal.read(c.lease["attempt_id"])["outcome"] == "intent"
        await c.worker.execute(c.lease)
        assert (c.backend.submitted, c.backend.reconciled) == (1, 1)
        assert sum(path.endswith("/dispatch") for _, path, _ in c.control.calls) == 1
        assert c.control.calls[-1][2]["outcome"] == "submission_uncertain"
    asyncio.run(scenario())


def test_lost_report_replays_receipt_without_resubmission(configured):
    async def scenario():
        c = configured
        c.control.fail_report = True
        with pytest.raises(ControlError):
            await c.worker.execute(c.lease)
        await c.worker.execute(c.lease)
        assert (c.backend.submitted, c.backend.reconciled) == (1, 0)
        assert c.journal.read(c.lease["attempt_id"])["outcome"] == "submitted"
    asyncio.run(scenario())


def test_terminal_output_failure_keeps_final_accounting(configured):
    async def scenario():
        c = configured
        c.lease.update(operation="observe", native_identity={"job_id": "17"})
        c.backend.output_failure = True
        await c.worker.execute(c.lease)
        report = c.control.calls[-1][2]
        assert report["observation"]["accounting"]["final"] and report["artifacts"] == {}
        assert report["error_code"] == "OUTPUT_COLLECTION_FAILED" and "private" not in str(report)
    asyncio.run(scenario())


def test_tampered_package_never_dispatches_and_poll_explicitly_clears_sync_extensions(configured):
    async def scenario():
        c = configured
        c.lease["prepared"]["files"]["main.py"] = base64.b64encode(b"modified").decode()
        with pytest.raises(ValueError):
            await c.worker.execute(c.lease)
        assert c.control.calls == [] and c.backend.submitted == 0
        assert await c.worker.poll_once() is False
        assert c.control.calls[0][2] == {"profiles": [], "extensions": []}
    asyncio.run(scenario())


def test_one_owner_serializes_while_other_owners_overlap_with_explicit_bounds():
    async def scenario():
        scheduler = PrincipalScheduler(max_concurrency=2, max_pending=3, max_principals=2)
        alice, bob = scheduler.register(uuid4()), scheduler.register(uuid4())
        with pytest.raises(WorkerCapacityError, match="registry"):
            scheduler.register(uuid4())
        release, started = asyncio.Event(), asyncio.Event()
        counts, peaks, total, peak = {alice: 0, bob: 0}, {alice: 0, bob: 0}, 0, 0

        async def operation(owner):
            nonlocal total, peak
            counts[owner] += 1
            total += 1
            peaks[owner], peak = max(peaks[owner], counts[owner]), max(peak, total)
            if total == 2:
                started.set()
            try:
                await release.wait()
            finally:
                counts[owner] -= 1
                total -= 1

        tasks = [asyncio.create_task(scheduler.run(owner, lambda owner=owner: operation(owner))) for owner in (alice, alice, bob)]
        await asyncio.wait_for(started.wait(), timeout=1)
        with pytest.raises(WorkerCapacityError, match="capacity"):
            await scheduler.run(bob, lambda: operation(bob))
        release.set()
        await asyncio.gather(*tasks)
        assert peaks == {alice: 1, bob: 1} and peak == 2
        with pytest.raises(WorkerCapacityError, match="not registered"):
            await scheduler.run(uuid4(), lambda: operation(bob))
    asyncio.run(scenario())


def test_waiting_owner_cancellation_releases_capacity_without_running_its_mutation():
    async def scenario():
        scheduler = PrincipalScheduler(max_concurrency=1, max_pending=2)
        owner = scheduler.register(uuid4())
        started, release = asyncio.Event(), asyncio.Event()

        async def held():
            started.set()
            await release.wait()

        async def forbidden():
            pytest.fail("Cancelled queued mutation executed")

        first = asyncio.create_task(scheduler.run(owner, held))
        await started.wait()
        second = asyncio.create_task(scheduler.run(owner, forbidden))
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        release.set()
        await first
        assert scheduler._pending == 0
    asyncio.run(scenario())


def test_async_journal_lock_keeps_cross_process_fence_without_blocking_loop(configured):
    async def scenario():
        journal, attempt = configured.journal, configured.lease["attempt_id"]
        progressed = False

        async def heartbeat():
            nonlocal progressed
            await asyncio.sleep(0.005)
            progressed = True

        with journal.locked(attempt):
            heartbeat_task = asyncio.create_task(heartbeat())
            with pytest.raises(TimeoutError):
                async with journal.async_locked(attempt, timeout=0.05):
                    pytest.fail("Existing process fence was bypassed")
            await heartbeat_task
        assert progressed
        async with journal.async_locked(attempt):
            pass
    asyncio.run(scenario())


def test_async_runner_rejects_sync_extension_hooks_at_configuration(configured):
    with pytest.raises(ValueError, match="synchronous worker"):
        AsyncWorker(configured.control, {}, configured.journal, owner_id=uuid4(), extensions=["storage"])
