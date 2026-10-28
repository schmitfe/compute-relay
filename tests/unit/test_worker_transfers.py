"""Publication backpressure stays bounded, cancellable and lease-fenced."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from compute_relay.worker import transfers
from compute_relay.worker.async_client import AsyncJsonClient
from compute_relay.worker.client import ControlError, _http_error
from compute_relay.worker.lease import renewing
from test_support.unit_test_async_worker import configured


def busy():
    return _http_error(409, b'{"error":{"code":"UPLOAD_BUSY","message":"private remote diagnostic"}}')


@pytest.mark.parametrize("code,status,attempts", [
    ("UPLOAD_BUSY", 409, 8), ("UPLOAD_BUSY", 403, 1), ("STORAGE_QUOTA", 409, 1),
    ("OBJECT_INTEGRITY", 409, 1), ("SCOPE_DENIED", 403, 1), ("CONTROL_UNAVAILABLE", None, 1),
])
def test_output_publication_retries_only_bounded_explicit_contention(monkeypatch, code, status, attempts):
    error = (_http_error(status, json.dumps({"error": {"code": code}}).encode())
        if status else ControlError(code))
    client = SimpleNamespace(request=AsyncMock(side_effect=error))
    sleep = AsyncMock()
    monkeypatch.setattr(transfers, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(transfers.random, "uniform", lambda lower, upper: upper)
    body = {"lease_id": "lease", "generation": 3, "transfer_id": "transfer", "offset": 12,
        "content_base64": "dGVzdA==", "finish": True}
    with pytest.raises(ControlError) as caught:
        asyncio.run(transfers._publish_output(client, "/output-chunk", body, asyncio.Lock()))
    assert caught.value is error and client.request.await_count == attempts
    assert all(call.args == ("POST", "/output-chunk", body) and call.args[2] is body
        for call in client.request.await_args_list)
    assert [call.args[0] for call in sleep.await_args_list] == ([.1, .2, .4, .8, 1.6, 2, 2] if attempts == 8 else [])


def test_output_publication_backoff_is_cancellable(monkeypatch):
    async def scenario():
        entered = asyncio.Event()

        async def sleep(delay):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(transfers, "asyncio", SimpleNamespace(sleep=sleep))
        client = SimpleNamespace(request=AsyncMock(side_effect=busy()))
        task = asyncio.create_task(transfers._publish_output(client, "/output-chunk", {}, asyncio.Lock()))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.request.await_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("renewal_fails", [False, True])
def test_collection_backoff_preserves_renewal_and_stops_when_authority_is_lost(monkeypatch, renewal_fails):
    monkeypatch.setattr("compute_relay.worker.lease.RENEWAL_INTERVAL_SECONDS", .001)

    async def scenario():
        renewals, publications = [], []
        lease = {"attempt_id": "attempt", "id": "lease", "generation": 3}

        async def request(method, path, body):
            if path.endswith("/renew"):
                renewals.append(body)
                if renewal_fails:
                    raise ControlError("STALE_LEASE", 409)
                return {"renewed": True}
            publications.append(body)
            if not renewals:
                raise busy()
            return {"complete": True, "offset": 0}

        async def sleep(delay):
            await asyncio.sleep(.02)

        monkeypatch.setattr(transfers, "asyncio", SimpleNamespace(sleep=sleep))
        client = SimpleNamespace(request=request)
        backend = SimpleNamespace(output_manifest=AsyncMock(return_value={"files": [
            {"path": "empty", "size_bytes": 0, "sha256": "a" * 64}]}))

        async def collect():
            async with renewing(client, lease):
                return await transfers.collect_outputs(client, backend, lease, {}, {"request": {"outputs": ["empty"]}}, asyncio.Lock())

        if renewal_fails:
            with pytest.raises(ControlError, match="LEASE_RENEWAL_FAILED"):
                await collect()
            assert len(publications) == 1
        else:
            result = await collect()
            assert result["collection_state"] == "complete" and len(publications) == 2
            assert publications[0] == publications[1]
        assert renewals and all(body == {"lease_id": "lease", "generation": 3} for body in renewals)

    asyncio.run(scenario())


def test_worker_queues_concurrent_publications_without_serializing_remote_reads(configured):
    async def scenario():
        active, peak, remote_reads = 0, 0, 0
        order = []
        all_reading = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            def __init__(self, body):
                self.body = body

            async def __aiter__(self):
                yield json.dumps(self.body).encode()

        async def handler(request):
            nonlocal active, peak
            body = json.loads(request.content)
            assert request.url.path.endswith("/output-chunk")
            order.append("chunk" if "transfer_id" in body else "descriptor")
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(.001)
                if active > 1:
                    return httpx.Response(409, stream=Stream({"error": {"code": "UPLOAD_BUSY"}}))
                return httpx.Response(200, stream=Stream({"complete": "transfer_id" in body,
                    "offset": 1 if "transfer_id" in body else 0, "transfer_id": "same-transfer"}))
            finally:
                active -= 1

        async def output_chunk(native, **kwargs):
            nonlocal remote_reads
            remote_reads += 1
            if remote_reads == 8:
                all_reading.set()
            await all_reading.wait()
            return {"offset": 0, "content_base64": "eA=="}

        backend = SimpleNamespace(supports_file_transfer=True, output_chunk=output_chunk,
            output_manifest=AsyncMock(return_value={"files": [
                {"path": "result", "size_bytes": 1, "sha256": "a" * 64, "version": "same"}]}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            configured.worker.client = AsyncJsonClient("http://127.0.0.1:8768", "synthetic-worker-token", http_client=http)

            async def finish():
                attempt = str(uuid4())
                native = {"attempt_id": attempt, "job_id": "17"}
                work = SimpleNamespace(attempt_id=attempt, manifest_sha256="b" * 64,
                    manifest={"request": {"outputs": ["result"]}})
                lease = {"attempt_id": attempt, "id": str(uuid4()), "generation": 1, "operation": "observe"}
                result = {"outcome": "observed", "native_identity": native,
                    "observation": {"terminal": True, "state": "completed", "accounting": {"final": True}}}
                await configured.worker._finish_observation(lease, work, native, backend, result)
                assert result["collection_state"] == "complete" and "error_code" not in result
                assert result["observation"]["accounting"]["final"] is True

            await asyncio.wait_for(asyncio.gather(*(finish() for _ in range(8))), timeout=2)
        assert peak == 1 and remote_reads == 8 and order == ["descriptor"] * 8 + ["chunk"] * 8

    asyncio.run(scenario())
