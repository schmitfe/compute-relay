"""Remote reclamation keeps identity/accounting and never follows output links."""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from django.utils import timezone

from compute_relay.control import remote_cleanup, services
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Attempt, LedgerEntry, WorkerReport
from compute_relay.control.models import Artifact, JobFile
from compute_relay.control.storage import store_uploaded_blob
from compute_relay.remote.bundles import bundle_source
from compute_relay.worker.client import ControlError
from test_support.unit_test_async_worker import Backend, configured
from test_support.unit_test_control import broker, budgeted, dispatched, prepared_job, submit_receipt
from test_support.unit_test_remote_bundles import prepared, site


@pytest.fixture
def remote(site):
    helper = {"__name__": "cleanup_fixture"}
    exec(compile(bundle_source("jobs"), "<fixed-cleanup>", "exec"), helper)
    config = site[0]
    package, files = prepared(site)
    calls = []
    native = {}

    def scheduler(config, command, arguments, **kwargs):
        calls.append(command)
        if command == "sbatch":
            return {"returncode": 0, "stdout": "42;fixture", "stderr": ""}
        timestamp = native["submitted_at"]
        row = (f"42|COMPLETED|1|1|cpu=1|0:0|{timestamp}|{timestamp}|{timestamp}|"
            f"broker-attempt:{native['attempt_id']}|{native['work_dir']}|\n")
        return {"returncode": 0, "stdout": "" if command == "squeue" else row, "stderr": ""}

    helper["scheduler"] = scheduler
    submitted = helper["submit"](config, {"attempt_id": str(uuid4()), "manifest": package["manifest"],
        "files": {path: base64.b64encode(data).decode() for path, data in files.items()}})
    native.update(submitted["native_identity"])
    request = {"operation": "cleanup", "protocol": "slurm-jobs/v1", "native_identity": native,
        "manifest_sha256": package["manifest_sha256"]}
    return helper, config, request, Path(native["work_dir"]), calls


def test_remote_cleanup_removes_only_attempt_and_replays_retained_journal(remote):
    helper, config, request, directory, calls = remote
    sibling = directory.parent / str(uuid4())
    sibling.mkdir()
    (sibling / "keep").write_text("other attempt")
    outside = directory.parents[3] / "outside"
    outside.write_text("must remain")
    (directory / "outward-link").symlink_to(outside)
    result = helper["execute_jobs"](config, request)
    assert result["state"] == "complete" and result["retained_journal"]
    assert result["size_bytes"] > 0 and not directory.exists()
    assert outside.read_text() == "must remain" and (sibling / "keep").read_text() == "other attempt"
    before = list(calls)
    assert helper["execute_jobs"](config, request) == result and calls == before
    journal = Path(config["remote_root"]) / "_broker_journal" / (request["native_identity"]["attempt_id"] + ".json")
    record = json.loads(journal.read_text())
    assert record["native_identity"] == request["native_identity"] and record["outcome"] == "submitted"
    assert record["cleanup"] == result and calls.count("sbatch") == 1


@pytest.mark.parametrize("change", ["empty", "running", "identity", "failed_query"])
def test_remote_cleanup_requires_exact_terminal_scheduler_evidence(remote, change):
    helper, config, request, directory, calls = remote
    original = helper["scheduler"]

    def scheduler(config, command, arguments, **kwargs):
        result = original(config, command, arguments, **kwargs)
        if command == "sacct":
            if change == "empty":
                result["stdout"] = ""
            elif change == "failed_query":
                result["returncode"] = 1
            elif change == "running":
                result["stdout"] = result["stdout"].replace("COMPLETED", "RUNNING")
            else:
                result["stdout"] = result["stdout"].replace("broker-attempt:", "other-attempt:")
        return result

    helper["scheduler"] = scheduler
    with pytest.raises(ValueError):
        helper["execute_jobs"](config, request)
    assert (directory / "main.py").exists()


@pytest.mark.parametrize("unsafe", ["hardlink", "fifo"])
def test_remote_cleanup_preflights_unsupported_entries_before_deletion(remote, unsafe):
    helper, config, request, directory, _ = remote
    if unsafe == "fifo":
        os.mkfifo(directory / "unsafe")
    else:
        os.link(directory / "main.py", directory / "unsafe")
    with pytest.raises(ValueError, match="unsupported"):
        helper["execute_jobs"](config, request)
    assert (directory / "main.py").exists() and (directory / "_broker/batch.sh").exists()


def test_remote_cleanup_resumes_interrupted_delete_without_resubmission(remote, monkeypatch):
    helper, config, request, directory, calls = remote
    original = os.unlink
    removed = []

    def interrupted(path, *args, **kwargs):
        if not str(path).startswith(".journal-"):
            if removed:
                raise OSError("fixture interruption")
            removed.append(path)
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", interrupted)
        with pytest.raises(OSError, match="fixture interruption"):
            helper["execute_jobs"](config, request)
    assert directory.exists() and removed
    assert helper["execute_jobs"](config, request)["state"] == "complete"
    assert not directory.exists() and calls.count("sbatch") == 1


def test_remote_cleanup_replays_after_directory_removal_before_receipt(remote, monkeypatch):
    helper, config, request, directory, calls = remote
    original = os.rmdir

    def interrupted(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == directory.name:
            raise OSError("fixture lost removal reply")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(os, "rmdir", interrupted)
        with pytest.raises(OSError, match="fixture lost removal reply"):
            helper["execute_jobs"](config, request)
    assert not directory.exists()
    assert helper["execute_jobs"](config, request)["state"] == "complete" and calls.count("sbatch") == 1


def test_concurrent_remote_cleanup_replays_one_durable_result(remote):
    helper, config, request, directory, calls = remote
    with ThreadPoolExecutor(max_workers=2) as threads:
        results = list(threads.map(lambda _: helper["execute_jobs"](config, request), range(2)))
    assert results[0] == results[1] and not directory.exists() and calls.count("sbatch") == 1


@pytest.mark.parametrize("limit", ["entries", "bytes"])
def test_remote_cleanup_traversal_is_bounded_before_removal(remote, limit):
    helper, _, _, directory, _ = remote
    budget = {"entries": 10000 if limit == "entries" else 0,
        "bytes": 2**63 - 1 if limit == "bytes" else 0, "device": directory.stat().st_dev}
    fd = helper["open_directory"](str(directory))
    try:
        with pytest.raises(ValueError, match="limit"):
            helper["cleanup_tree"](fd, budget=budget)
    finally:
        os.close(fd)
    assert (directory / "main.py").exists()


@pytest.mark.parametrize("change", ["manifest", "native", "extension", "symlink", "depth"])
def test_remote_cleanup_rejects_changed_or_unsafe_directory_bindings(remote, change):
    helper, config, request, directory, _ = remote
    if change == "manifest":
        request["manifest_sha256"] = "0" * 64
    elif change == "native":
        request["native_identity"] = {**request["native_identity"], "job_id": "43"}
    elif change == "extension":
        journal = Path(config["remote_root"]) / "_broker_journal" / (request["native_identity"]["attempt_id"] + ".json")
        record = json.loads(journal.read_text())
        record["manifest"]["extension_name"] = "environments"
        record["manifest_sha256"] = helper["digest"](helper["canonical"](record["manifest"]))
        journal.write_text(json.dumps(record))
        request["manifest_sha256"] = record["manifest_sha256"]
    elif change == "symlink":
        destination = directory.parent / "retained-real-attempt"
        directory.rename(destination)
        directory.symlink_to(destination, target_is_directory=True)
    else:
        nested = directory
        for _ in range(34):
            nested = nested / "deeper"
            nested.mkdir()
    with pytest.raises((OSError, ValueError)):
        helper["execute_jobs"](config, request)
    assert (directory / "main.py").exists()


def completed(broker):
    budgeted(broker)
    job = prepared_job(broker)
    attempt, lease = dispatched(broker, job)
    native, lease = submit_receipt(broker, attempt, lease)
    services.report(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "outcome": "observed", "native_identity": native, "collection_state": "complete", "observation": {
            "state": "completed", "terminal": True, "accounting": {"final": True, "cpu_core_seconds": 1, "gpu_device_seconds": 0}}})
    broker.connection.backend = "slurm"
    broker.connection.save(update_fields=["backend"])
    attempt.refresh_from_db()
    return job, attempt


def request_cleanup(broker, job):
    value = remote_cleanup.preview(broker.project, job)
    token = remote_cleanup.confirmation(broker.project, broker.owner, value, job)
    return remote_cleanup.queue(broker.project, broker.owner, token, job)


def test_owner_remote_cleanup_uses_fenced_lease_and_never_changes_compute_accounting(broker):
    job, attempt = completed(broker)
    ledger = list(LedgerEntry.objects.values_list("action", "cpu", "gpu"))
    assert request_cleanup(broker, job) == {"queued": 1}
    lease = services.lease_work(broker.worker)["lease"]
    assert lease["operation"] == "cleanup" and lease["prepared"]["files"] == {}
    identity = {"lease_id": lease["id"], "generation": lease["generation"]}
    assert services.dispatch(broker.worker, attempt.id, {**identity, "manifest_sha256": job.manifest_sha256,
        "connection_revision_sha256": broker.connection.revision_sha256}) == {"authorized": True}
    data = {**identity, "outcome": "cleaned", "native_identity": attempt.native_identity,
        "cleanup": {"state": "complete", "size_bytes": 123, "entries": 4, "retained_journal": True}}
    first = services.report(broker.worker, attempt.id, data)
    assert services.report(broker.worker, attempt.id, data) == first
    attempt.refresh_from_db()
    assert attempt.remote_cleanup["state"] == "complete" and attempt.accounting_state == "settled"
    assert list(LedgerEntry.objects.values_list("action", "cpu", "gpu")) == ledger
    assert WorkerReport.objects.filter(attempt=attempt, lease_id=lease["id"]).count() == 1
    assert services.lease_work(broker.worker)["lease"] is None


@pytest.mark.parametrize("protected", ["accounting", "collection", "active_lease", "extension", "inactive_owner"])
def test_remote_cleanup_protects_unresolved_or_managed_attempts(broker, protected):
    job, attempt = completed(broker)
    if protected == "accounting":
        Attempt.objects.filter(id=attempt.id).update(accounting_state="settlement_pending")
    elif protected == "collection":
        Attempt.objects.filter(id=attempt.id).update(collection_pending=True)
    elif protected == "active_lease":
        Attempt.objects.filter(id=attempt.id).update(lease_expires_at=timezone.now() + timedelta(minutes=1))
    elif protected == "inactive_owner":
        broker.owner.is_active = False
        broker.owner.save(update_fields=["is_active"])
    else:
        job.extension_name = "environments"
        job.save(update_fields=["extension_name"])
    assert request_cleanup(broker, job) == {"queued": 0}


def test_remote_cleanup_rechecks_protection_when_worker_receives_queued_work(broker):
    job, attempt = completed(broker)
    assert request_cleanup(broker, job) == {"queued": 1}
    Attempt.objects.filter(id=attempt.id).update(collection_pending=True)
    assert services.lease_work(broker.worker)["lease"] is None
    attempt.refresh_from_db()
    assert attempt.remote_cleanup == {"state": "failed", "error_code": "CLEANUP_PREPARATION_FAILED"}


def test_remote_cleanup_rechecks_new_result_reference_at_dispatch(broker):
    consumer = prepared_job(broker)
    job, attempt = completed(broker)
    assert request_cleanup(broker, job) == {"queued": 1}
    lease = services.lease_work(broker.worker)["lease"]
    result = store_uploaded_blob(broker.project, b"synthetic result to retain")
    artifact = Artifact.objects.create(attempt=attempt, path="result", blob=result)
    JobFile.objects.create(job=consumer, path="result", blob=result, source_kind="artifact", source_artifact=artifact)
    with pytest.raises(BrokerError) as denied:
        services.dispatch(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
            "manifest_sha256": job.manifest_sha256, "connection_revision_sha256": broker.connection.revision_sha256})
    assert denied.value.code == "CLEANUP_PROTECTED"


def test_remote_cleanup_confirmation_cannot_cross_owner_or_job_scope(broker):
    job, attempt = completed(broker)
    value = remote_cleanup.preview(broker.project, job)
    token = remote_cleanup.confirmation(broker.project, broker.owner, value, job)
    with pytest.raises(BrokerError) as denied:
        remote_cleanup.queue(broker.project, broker.outsider, token, job)
    assert denied.value.code == "SCOPE_DENIED"
    with pytest.raises(BrokerError) as changed:
        remote_cleanup.queue(broker.project, broker.owner, token)
    assert changed.value.code == "CLEANUP_PREVIEW_CHANGED"
    with pytest.raises(BrokerError) as expired:
        remote_cleanup.queue(broker.project, broker.owner, "invalid", job)
    assert expired.value.code == "CLEANUP_PREVIEW_EXPIRED"
    attempt.refresh_from_db()
    assert attempt.remote_cleanup == {}


def test_cleanup_worker_replays_lost_report_without_deleting_twice(configured):
    async def scenario():
        c = configured
        c.lease.update(operation="cleanup", native_identity={"job_id": "17", "attempt_id": c.lease["attempt_id"]})

        class Cleaner(Backend):
            cleaned = 0

            async def cleanup(self, native, *, manifest):
                self.cleaned += 1
                return {"state": "complete", "size_bytes": 123, "entries": 4, "retained_journal": True}

        backend = Cleaner()
        c.worker.backend_factory = lambda _: backend
        c.control.fail_report = True
        with pytest.raises(ControlError):
            await c.worker.execute(c.lease)
        await c.worker.execute(c.lease)
        assert backend.cleaned == 1 and backend.submitted == 0
        assert sum(path.endswith("/dispatch") for _, path, _ in c.control.calls) == 1
        assert c.journal.read_report(c.lease["attempt_id"]) is None

    asyncio.run(scenario())
