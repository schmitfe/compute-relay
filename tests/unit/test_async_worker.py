"""Async owner lanes and durable submission fencing without remote execution."""
from test_support.unit_test_async_worker import configured

import asyncio
import base64
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from compute_relay.backends import canonical_json_bytes, prepare
from compute_relay.worker.runner import Worker
from compute_relay.worker.client import ControlError
from compute_relay.worker.concurrency import PrincipalScheduler, WorkerCapacityError
from compute_relay.worker.journal import Journal


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

    async def observe(self, native, *, manifest):
        return {"state": "completed", "terminal": True, "accounting": {"cpu_core_seconds": 2, "gpu_device_seconds": 0, "final": True}}

    async def cancel(self, native, *, manifest):
        self.cancelled += 1
        return {"outcome": "cancel_requested"}

    async def collect(self, native, outputs, *, manifest):
        if self.output_failure:
            raise OSError("private remote diagnostic")
        return {"stdout": "synthetic result", "stderr": "", "artifacts": {}}




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
        assert c.control.calls[0][2] == {"profiles": [], "extensions": [], "execution_features": ["availability_v1"]}
    asyncio.run(scenario())


def test_one_owner_serializes_while_other_owners_overlap_with_explicit_bounds():
    async def scenario():
        scheduler = PrincipalScheduler(max_concurrency=2, max_pending=3, max_principals=2, max_per_owner=1)
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


def test_runner_advertises_only_installed_native_async_extension_hooks(configured):
    worker = Worker(configured.control, {}, configured.journal, owner_id=uuid4(), extensions=["storage", "environments", "workflows"])
    asyncio.run(worker.publish_profiles())
    assert configured.control.calls[-1][2] == {"profiles": [], "extensions": ["storage", "environments", "workflows"], "execution_features": ["availability_v1"]}


def extension_worker(configured, name, *, enabled=True):
    c = configured
    manifest = c.lease["prepared"]["manifest"]
    manifest["extension_name"] = name
    c.lease["prepared"]["manifest_sha256"] = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    connection = c.worker.connections[c.lease["connection_id"]]
    connection.update(backend="slurm", remote_root="/trusted/project/jobs")
    c.backend.connection = connection
    c.worker = Worker(c.control, {c.lease["connection_id"]: connection}, c.journal,
        owner_id=uuid4(), backend_factory=lambda _: c.backend, extensions=[name] if enabled else [])
    return c


@pytest.mark.parametrize("name", ["environments", "workflows"])
def test_async_worker_submits_enabled_extension_packages_with_normal_dispatch_and_intent(configured, name):
    c = extension_worker(configured, name)
    asyncio.run(c.worker.execute(c.lease))
    assert c.backend.submitted == 1
    assert sum(path.endswith("/dispatch") for _, path, _ in c.control.calls) == 1
    assert c.journal.read(c.lease["attempt_id"])["outcome"] == "submitted"


def test_disabled_extension_package_never_dispatches_or_writes_an_intent(configured):
    c = extension_worker(configured, "environments", enabled=False)
    with pytest.raises(ValueError, match="not enabled"):
        asyncio.run(c.worker.execute(c.lease))
    assert c.control.calls == [] and c.backend.submitted == 0
    assert c.journal.read(c.lease["attempt_id"]) is None


def environment_context(c):
    context = {"build_id": str(uuid4()), "attempt_id": c.lease["attempt_id"], "spec_sha256": "a" * 64, "generation": 4}
    c.lease.update(operation="observe", native_identity={"job_id": "17", "attempt_id": c.lease["attempt_id"]},
        extension_context={"environments": context})
    return context


def test_async_environment_terminal_hook_finalizes_bound_metadata_and_collects_outputs(configured, monkeypatch):
    from compute_relay.extensions.environments.remote import REMOTE_SOURCE
    c = extension_worker(configured, "environments")
    context = environment_context(c)
    calls = []
    async def call_helper(source, request):
        await asyncio.sleep(0)
        calls.append((source, request))
        return {"verified": True, "spec_sha256": context["spec_sha256"]}
    c.backend.transport = SimpleNamespace(call_helper=call_helper)
    monkeypatch.setattr(asyncio, "to_thread", lambda *a, **kw: pytest.fail("Async finalization used a thread-pool SSH bridge"))
    asyncio.run(c.worker.execute(c.lease))
    report = c.control.calls[-1][2]
    assert report["extension_data"]["environments"] == {"environment_finalization": {
        "verified": True, "spec_sha256": context["spec_sha256"]}, "generation": 4}
    assert report["observation"]["accounting"]["final"] is True and report["stdout"] == "synthetic result"
    assert calls == [(REMOTE_SOURCE, {"operation": "finalize_environment", "config": {"remote_root": "/trusted/project/jobs"},
        "native_identity": c.lease["native_identity"], "build_id": context["build_id"], "spec_sha256": "a" * 64})]


@pytest.mark.parametrize("failure", ["attempt-mismatch", "remote-failure", "hook-failure"])
def test_async_environment_finalization_failure_preserves_core_accounting_and_output_collection(configured, monkeypatch, failure):
    from compute_relay import extensions
    c = extension_worker(configured, "environments")
    context = environment_context(c)
    calls = []
    async def call_helper(source, request):
        calls.append(request)
        raise OSError("synthetic-private-ssh-error")
    c.backend.transport = SimpleNamespace(call_helper=call_helper)
    if failure == "attempt-mismatch":
        context["attempt_id"] = str(uuid4())
    elif failure == "hook-failure":
        async def failed_hook(*args):
            raise OSError("synthetic-private-extension-error")
        monkeypatch.setattr(extensions, "worker_after_observation", failed_hook)
    asyncio.run(c.worker.execute(c.lease))
    report = c.control.calls[-1][2]
    assert report["observation"]["accounting"]["final"] is True and report["stdout"] == "synthetic result"
    if failure == "hook-failure":
        assert report["extension_data"] == {"environments": {"error": "FINALIZATION_UNAVAILABLE"}}
    else:
        assert report["extension_data"]["environments"]["environment_finalization"] == {
            "verified": False, "error_code": "ENVIRONMENT_METADATA_UNVERIFIED"}
    if failure != "remote-failure":
        assert calls == []
    assert "synthetic-private" not in str(report)


def test_async_environment_cancellation_propagates_without_reporting_completion(configured):
    c = extension_worker(configured, "environments")
    environment_context(c)
    entered = asyncio.Event()
    async def call_helper(source, request):
        entered.set()
        await asyncio.Event().wait()
    c.backend.transport = SimpleNamespace(call_helper=call_helper)
    async def scenario():
        task = asyncio.create_task(c.worker.execute(c.lease))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert c.control.calls == []


def test_disabled_async_terminal_hook_never_imports_extension_and_still_collects(configured, monkeypatch):
    from compute_relay import extensions
    c = extension_worker(configured, "environments", enabled=False)
    environment_context(c)
    monkeypatch.setattr(extensions, "import_module", lambda *a: pytest.fail("Disabled extension was imported"))
    asyncio.run(c.worker.execute(c.lease))
    report = c.control.calls[-1][2]
    assert "extension_data" not in report and report["stdout"] == "synthetic result"
    assert report["observation"]["accounting"]["final"] is True


def test_disabled_async_maintenance_is_rejected_before_import(configured, monkeypatch):
    from compute_relay import extensions
    monkeypatch.setattr(extensions, "import_module", lambda *a: pytest.fail("Disabled maintenance extension was imported"))
    with pytest.raises(ValueError, match="not enabled"):
        asyncio.run(configured.worker.execute({"operation": "extension", "extension": "storage"}))
    assert configured.control.calls == []


def test_two_hundred_jobs_dispatch_with_four_bounded_same_owner_operations(configured):
    from collections import deque
    from copy import deepcopy
    from compute_relay.worker.runner import serve_workers
    async def scenario():
        c = configured
        leases = deque({**deepcopy(c.lease), "id": str(uuid4()), "attempt_id": str(uuid4())} for _ in range(200))
        expected = {lease["attempt_id"] for lease in leases}
        done = asyncio.Event()
        reports, dispatched, active, peak, submitted = set(), set(), 0, 0, 0
        gate = asyncio.Event()
        class QueueControl(Control):
            async def request(self, method, path, body=None):
                if path == "/internal/v1/lease":
                    assert len(body["in_progress"]) <= 4
                    assert body["core_only"] == bool(body["in_progress"])
                    self.calls.append((method, path, body))
                    await asyncio.sleep(0)
                    return {"lease": leases.popleft() if leases else None}
                if path.endswith("/dispatch"):
                    attempt = path.split("/")[-2]
                    assert attempt not in dispatched
                    dispatched.add(attempt)
                result = await super().request(method, path, body)
                if path.endswith("/report"):
                    reports.add(path.split("/")[-2])
                    if len(reports) == 200:
                        done.set()
                return result
        class ParallelBackend(Backend):
            async def submit(self, attempt_id, manifest, files):
                nonlocal active, peak, submitted
                active += 1; submitted += 1
                native = str(submitted)
                peak = max(peak, active)
                if active == 4:
                    gate.set()
                try:
                    await gate.wait()
                    await asyncio.sleep(0)
                    return {"outcome": "submitted", "native_identity": {"job_id": native, "attempt_id": attempt_id}}
                finally:
                    active -= 1
        c.worker.client = QueueControl()
        backend = ParallelBackend()
        c.worker.backend_factory = lambda _: backend
        task = asyncio.create_task(serve_workers([c.worker]))
        try:
            await asyncio.wait_for(done.wait(), timeout=60)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert peak == 4 and active == 0 and not c.worker._in_progress
        assert submitted == 200 and reports == dispatched == expected
        assert not list(c.journal.pending_reports())
    asyncio.run(scenario())


def test_same_attempt_concurrency_cannot_duplicate_submission(configured):
    async def scenario():
        c = configured
        await asyncio.gather(c.worker.execute(c.lease), c.worker.execute(c.lease))
        assert c.backend.submitted == 1
        assert sum(path.endswith("/dispatch") for _, path, _ in c.control.calls) == 1
    asyncio.run(scenario())


def test_parallel_shutdown_preserves_every_intent_and_resume_only_reconciles(configured):
    from collections import deque
    from copy import deepcopy
    from compute_relay.worker.runner import serve_workers
    async def scenario():
        c = configured
        proposals = [{**deepcopy(c.lease), "id": str(uuid4()), "attempt_id": str(uuid4())} for _ in range(4)]
        queue = deque(proposals)
        ready = asyncio.Event()
        class QueueControl(Control):
            async def request(self, method, path, body=None):
                if path == "/internal/v1/lease":
                    return {"lease": queue.popleft() if queue else None}
                return await super().request(method, path, body)
        class BlockingBackend(Backend):
            async def submit(self, attempt_id, manifest, files):
                self.submitted += 1
                if self.submitted == 4:
                    ready.set()
                await asyncio.Event().wait()
        c.worker.client = QueueControl()
        backend = BlockingBackend()
        c.worker.backend_factory = lambda _: backend
        serving = asyncio.create_task(serve_workers([c.worker]))
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
        finally:
            serving.cancel()
            with pytest.raises(asyncio.CancelledError):
                await serving
        assert not c.worker._in_progress
        assert all(c.journal.read(lease["attempt_id"])["outcome"] == "intent" for lease in proposals)
        await asyncio.gather(*(c.worker.execute(lease) for lease in proposals))
        assert backend.submitted == backend.reconciled == 4
        assert sum(path.endswith("/dispatch") for _, path, _ in c.worker.client.calls) == 4
    asyncio.run(scenario())


def test_parallel_poller_cancels_other_lanes_before_propagating_error():
    from compute_relay.worker.concurrency import poll_parallel
    async def scenario():
        active = 0
        ready = asyncio.Event()
        async def poll():
            nonlocal active
            active += 1
            number = active
            if active == 4:
                ready.set()
            try:
                await ready.wait()
                if number == 4:
                    raise ControlError("CONTROL_HTTP_ERROR", 403)
                await asyncio.Event().wait()
            finally:
                active -= 1
        with pytest.raises(ControlError):
            await asyncio.wait_for(poll_parallel(poll, concurrency=4), timeout=2)
        assert active == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, 9, -1, True, 1.5, "4"])
def test_explicit_remote_concurrency_is_bounded(value):
    from compute_relay.worker.concurrency import remote_parallelism
    with pytest.raises(ValueError):
        remote_parallelism(value)


def test_two_hundred_observations_use_four_batches_and_preserve_each_terminal_report(configured):
    from collections import deque
    from copy import deepcopy
    from compute_relay.worker.runner import serve_workers
    async def scenario():
        c = configured
        c.worker.observation_batch = 64
        leases = [{**deepcopy(c.lease), "id": str(uuid4()), "attempt_id": str(uuid4()), "operation": "observe"} for _ in range(200)]
        for index, lease in enumerate(leases):
            lease["native_identity"] = {"job_id": str(index + 1), "attempt_id": lease["attempt_id"]}
        queue = deque(leases)
        done = asyncio.Event()
        reports, batches = set(), []
        class QueueControl(Control):
            async def request(self, method, path, body=None):
                if path == "/internal/v1/lease":
                    assert body["observation_batch"] == 64 and len(body["in_progress"]) <= 256
                    group = [queue.popleft() for _ in range(min(64, len(queue)))]
                    return {"lease": group[0], "observations": group[1:]} if group else {"lease": None}
                if path.endswith("/renew"):
                    return {"renewed": True}
                if path.endswith("/report"):
                    attempt = path.split("/")[-2]
                    assert attempt not in reports and body["observation"]["accounting"]["final"]
                    reports.add(attempt)
                    if len(reports) == 200:
                        done.set()
                return await super().request(method, path, body)
        class BatchedBackend(Backend):
            async def observe_many(self, items):
                batches.append(len(items))
                return {item["native_identity"]["attempt_id"]: await self.observe(item["native_identity"], manifest=item["manifest"]) for item in items}
        c.worker.client = QueueControl()
        c.worker.backend_factory = lambda _: BatchedBackend()
        serving = asyncio.create_task(serve_workers([c.worker]))
        try:
            await asyncio.wait_for(done.wait(), timeout=60)
        finally:
            serving.cancel()
            with pytest.raises(asyncio.CancelledError):
                await serving
        assert sorted(batches) == [8, 64, 64, 64]
        assert len(reports) == 200 and not c.worker._in_progress and not list(c.journal.pending_reports())
    asyncio.run(scenario())


def test_completed_batch_item_stops_renewing_while_other_items_collect(configured, monkeypatch):
    from copy import deepcopy
    from compute_relay.worker import lease as lease_module
    async def scenario():
        c = configured
        reported = set()
        proposals = [{**deepcopy(c.lease), "operation": "observe", "id": str(uuid4()), "attempt_id": str(uuid4())} for _ in range(2)]
        for index, proposal in enumerate(proposals):
            proposal["native_identity"] = {"job_id": str(index + 1), "attempt_id": proposal["attempt_id"]}
        class RenewalControl(Control):
            async def request(self, method, path, body=None):
                attempt = path.split("/")[-2]
                if path.endswith("/renew"):
                    assert attempt not in reported
                    return {"renewed": True}
                if path.endswith("/report"):
                    reported.add(attempt)
                return await super().request(method, path, body)
        class SlowCollection(Backend):
            async def observe_many(self, items):
                return {item["native_identity"]["attempt_id"]: await self.observe(item["native_identity"], manifest=item["manifest"]) for item in items}
            async def collect(self, native, outputs, *, manifest):
                if native["job_id"] == "2":
                    await asyncio.sleep(.06)
                return await super().collect(native, outputs, manifest=manifest)
        c.worker.client = RenewalControl()
        c.worker.backend_factory = lambda _: SlowCollection()
        monkeypatch.setattr(lease_module, "RENEWAL_INTERVAL_SECONDS", .01)
        await c.worker._execute_observations(proposals)
        assert len(reported) == 2
    asyncio.run(scenario())


def test_new_cancellation_lease_survives_older_batch_cleanup(configured):
    from copy import deepcopy
    from compute_relay.worker.runner import serve_workers
    async def scenario():
        c = configured
        c.worker.observation_batch = 2
        first, second = [{**deepcopy(c.lease), "operation": "observe", "id": str(uuid4()), "attempt_id": str(uuid4())} for _ in range(2)]
        for index, proposal in enumerate((first, second)):
            proposal["native_identity"] = {"job_id": str(index + 1), "attempt_id": proposal["attempt_id"]}
        cancellation = {**deepcopy(first), "id": str(uuid4()), "generation": 2, "operation": "cancel"}
        first_reported = False
        issued_batch = issued_cancel = False
        cancel_started, release_cancel, release_second, old_finished, done = [asyncio.Event() for _ in range(5)]
        class QueueControl(Control):
            async def request(self, method, path, body=None):
                nonlocal issued_batch, issued_cancel, first_reported
                if path == "/internal/v1/lease":
                    if not issued_batch:
                        issued_batch = True
                        return {"lease": first, "observations": [second]}
                    if first_reported and not issued_cancel and first["attempt_id"] not in body["in_progress"]:
                        issued_cancel = True
                        return {"lease": cancellation}
                    return {"lease": None}
                if path.endswith("/report"):
                    if body["lease_id"] == first["id"]:
                        first_reported = True
                    if body["lease_id"] == cancellation["id"]:
                        done.set()
                return await super().request(method, path, body)
        class SlowBackend(Backend):
            async def observe_many(self, items):
                return {first["attempt_id"]: {"state": "running", "terminal": False},
                    second["attempt_id"]: await self.observe(second["native_identity"], manifest=items[1]["manifest"])}
            async def collect(self, native, outputs, *, manifest):
                await release_second.wait()
                return await super().collect(native, outputs, manifest=manifest)
            async def cancel(self, native, *, manifest):
                cancel_started.set()
                await release_cancel.wait()
                return await super().cancel(native, manifest=manifest)
        c.worker.client = QueueControl()
        c.worker.backend_factory = lambda _: SlowBackend()
        original = c.worker._execute_observations
        async def tracked(leases):
            try:
                return await original(leases)
            finally:
                old_finished.set()
        c.worker._execute_observations = tracked
        serving = asyncio.create_task(serve_workers([c.worker]))
        try:
            await asyncio.wait_for(cancel_started.wait(), timeout=5)
            release_second.set()
            await asyncio.wait_for(old_finished.wait(), timeout=2)
            assert c.worker._in_progress[first["attempt_id"]] == cancellation["id"]
            release_cancel.set()
            await asyncio.wait_for(done.wait(), timeout=2)
        finally:
            serving.cancel()
            with pytest.raises(asyncio.CancelledError):
                await serving
    asyncio.run(scenario())
