"""Native async storage probes keep the same protected context and root fences."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from compute_relay.extensions.storage import services
from compute_relay.extensions.storage.remote import REMOTE_SOURCE
from compute_relay.worker.runner import Worker
from compute_relay.worker.journal import Journal
from test_support.extensions_storage_test_storage_probes import lease, probes, sample


class AsyncControl:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, body):
        await asyncio.sleep(0)
        self.calls.append((method, path, body))
        return {"accepted": True}


def worker_for(probes, tmp_path, transport):
    client = AsyncControl()
    configurations = []
    def backend(configuration):
        configurations.append(configuration)
        return SimpleNamespace(transport=transport)
    worker = Worker(client, {}, Journal(tmp_path / "async-journal"), owner_id=probes.owner.id,
        credential_profiles={"protected": probes.profile}, extensions=["storage"], backend_factory=backend)
    return worker, client, configurations


def test_async_probe_awaits_fixed_helper_and_reports_exact_lease_binding(probes, tmp_path, monkeypatch):
    document = lease(probes)
    calls = []
    async def helper(source, request):
        await asyncio.sleep(0)
        calls.append((source, request))
        return {"context": deepcopy(request["context"]), "observation": sample()}
    worker, control, configurations = worker_for(probes, tmp_path, SimpleNamespace(call_helper=helper))
    monkeypatch.setattr(asyncio, "to_thread", lambda *a, **kw: pytest.fail("Async maintenance used a thread-pool SSH bridge"))
    asyncio.run(worker.execute(document))
    assert calls[0][0] == REMOTE_SOURCE
    assert calls[0][1] == {"operation": "probe_storage", "context": document["context"],
        "root": str(probes.root), "expected_filesystem_id": None}
    assert configurations[0]["command_timeout_seconds"] <= 30
    method, path, report = control.calls[0]
    assert method == "POST" and path == f"/internal/v1/extensions/storage/pools/{probes.target.pool_id}/report"
    assert report["lease_id"] == document["id"] and report["generation"] == document["generation"]
    assert report["target_id"] == document["context"]["target_id"]
    assert report["observation"]["identity_verified"] is False
    assert services.report_probe(probes.worker, probes.target.pool_id, report)["accepted"] is True
    assert "identity_file" not in str(report)


@pytest.mark.parametrize("change", ["root_alias", "profile_revision_sha256", "connection_revision_sha256"])
def test_async_probe_rejects_unapproved_root_or_stale_binding_before_ssh(probes, tmp_path, change):
    document = lease(probes)
    document["context"][change] = "not-approved" if change == "root_alias" else "f" * 64
    async def forbidden(*args):
        pytest.fail("Unbound probe reached SSH")
    worker, control, configurations = worker_for(probes, tmp_path, SimpleNamespace(call_helper=forbidden))
    asyncio.run(worker.execute(document))
    assert configurations == []
    assert control.calls[0][2]["observation"]["error_code"] == "STORAGE_PROBE_UNAVAILABLE"


@pytest.mark.parametrize("failure", ["wrong-context", "remote-error", "invalid-observation"])
def test_async_probe_failures_report_unknown_capacity_without_leaking_diagnostics(probes, tmp_path, failure):
    document = lease(probes)
    async def helper(source, request):
        if failure == "remote-error":
            raise OSError("synthetic-private-ssh-detail")
        context = deepcopy(request["context"])
        observation = sample()
        if failure == "wrong-context":
            context["root_alias"] = "other-root"
        else:
            observation["available_bytes"] = "synthetic-private-invalid-value"
        return {"context": context, "observation": observation}
    worker, control, _ = worker_for(probes, tmp_path, SimpleNamespace(call_helper=helper))
    asyncio.run(worker.execute(document))
    report = control.calls[0][2]
    assert report["observation"]["error_code"] == "STORAGE_PROBE_UNAVAILABLE"
    assert report["observation"]["available_bytes"] is None
    assert "synthetic-private" not in str(report)


def test_async_probe_rejects_arbitrary_path_fields_without_sending_a_report(probes, tmp_path):
    document = lease(probes)
    document["context"]["root"] = "/unapproved/path"
    worker, control, configurations = worker_for(probes, tmp_path, None)
    with pytest.raises(ValueError, match="context"):
        asyncio.run(worker.execute(document))
    assert configurations == [] and control.calls == []


def test_async_probe_cancellation_keeps_the_lease_unreported(probes, tmp_path):
    document = lease(probes)
    entered = asyncio.Event()
    async def helper(source, request):
        entered.set()
        await asyncio.Event().wait()
    worker, control, _ = worker_for(probes, tmp_path, SimpleNamespace(call_helper=helper))
    async def scenario():
        task = asyncio.create_task(worker.execute(document))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert control.calls == []
