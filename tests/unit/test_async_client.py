import asyncio

import httpx
import pytest

from compute_relay.worker.async_client import AsyncJsonClient
from compute_relay.worker.client import ControlError


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, entered=None, release=None):
        self.chunks, self.closed = chunks, False
        self.entered, self.release = entered, release

    async def __aiter__(self):
        if self.entered:
            self.entered.set()
        if self.release:
            await self.release.wait()
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def test_async_http_is_bounded_strict_and_does_not_forward_credentials_through_redirects():
    async def scenario():
        seen = []

        async def handler(request):
            seen.append(request)
            if request.url.path == "/redirect":
                return httpx.Response(302, headers={"Location": "https://elsewhere.invalid/capture"}, stream=Stream([b"private"]))
            if request.url.path == "/large":
                return httpx.Response(200, stream=Stream([b"x" * 4096]))
            if request.url.path == "/compressed":
                return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=Stream([b"invalid gzip"]))
            if request.url.path == "/invalid":
                return httpx.Response(200, stream=Stream([b'{"value":1,"value":2}']))
            return httpx.Response(200, stream=Stream([b'{"ok":true}']))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = AsyncJsonClient("https://control.invalid", "synthetic-worker-token", http_client=http, max_response_bytes=2048)
            assert await client.request("POST", "/ok", {"operation": "lease"}, idempotency_key="stable-1") == {"ok": True}
            assert seen[0].headers["Authorization"] == "Bearer synthetic-worker-token"
            assert seen[0].headers["Accept-Encoding"] == "identity"
            assert seen[0].headers["Idempotency-Key"] == "stable-1"
            for path, code in (("/redirect", "CONTROL_HTTP_ERROR"), ("/large", "RESPONSE_TOO_LARGE"),
                               ("/compressed", "INVALID_CONTROL_RESPONSE"), ("/invalid", "INVALID_CONTROL_RESPONSE")):
                with pytest.raises(ControlError) as error:
                    await client.request("GET", path)
                assert error.value.code == code and "private" not in str(error.value)
            assert len(seen) == 5 and all(request.url.host == "control.invalid" for request in seen)
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["http://university.example", "https://token@control.invalid", "https://control.invalid/path"])
def test_async_http_rejects_remote_cleartext_credentials_and_path_origins(url):
    with pytest.raises(ValueError):
        AsyncJsonClient(url, "synthetic-worker-token")


def test_async_http_cancellation_closes_stream_without_retrying():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        stream = Stream([b'{"ok":true}'], entered=entered, release=release)
        seen = 0

        async def handler(request):
            nonlocal seen
            seen += 1
            return httpx.Response(200, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = AsyncJsonClient("https://control.invalid", "synthetic-worker-token", http_client=http)
            task = asyncio.create_task(client.request("POST", "/dispatch", {}))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stream.closed and seen == 1
    asyncio.run(scenario())


def test_async_http_total_stream_timeout_is_not_an_unbounded_per_chunk_timeout():
    async def scenario():
        stream = Stream([], release=asyncio.Event())
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))) as http:
            client = AsyncJsonClient("https://control.invalid", "synthetic-worker-token", http_client=http, timeout=0.02)
            with pytest.raises(ControlError) as error:
                await client.request("GET", "/lease")
            assert error.value.code == "CONTROL_UNAVAILABLE" and stream.closed
    asyncio.run(scenario())


def test_async_http_errors_preserve_only_safe_codes_and_bounded_numeric_diagnostics():
    import json
    from test_resource_limit_client import PRIVATE, TOKEN, assert_private_redacted, resource_error

    async def scenario():
        bodies = [resource_error(), {"error": {"code": "SCHEDULER_OPTIONS_REQUIRED", "message": PRIVATE + TOKEN}},
                  {"error": {"code": "UPLOAD_BUSY", "message": PRIVATE + TOKEN, "details": {"private": PRIVATE + TOKEN}}},
                  {"error": {"code": PRIVATE + TOKEN, "message": PRIVATE + TOKEN}}]
        for document in bodies:
            stream = Stream([json.dumps(document).encode()])
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(400, stream=stream))) as http:
                client = AsyncJsonClient("https://control.invalid", TOKEN, http_client=http)
                with pytest.raises(ControlError) as caught:
                    await client.request("POST", "/probe", {})
                error = caught.value
                expected = document["error"]["code"] if document["error"]["code"] != PRIVATE + TOKEN else None
                assert error.broker_code == expected and error.status == 400 and error.code == "CONTROL_HTTP_ERROR"
                assert_private_redacted(error)
                assert stream.closed
        for maximum, compressed in ((32, False), (4 * 1024 * 1024, False), (4 * 1024 * 1024, True)):
            stream = Stream([json.dumps(bodies[0]).encode() + b" " * 70000])
            headers = {"Content-Encoding": "gzip"} if compressed else {}
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(400, headers=headers, stream=stream))) as http:
                client = AsyncJsonClient("https://control.invalid", TOKEN, http_client=http, max_response_bytes=maximum)
                with pytest.raises(ControlError) as caught:
                    await client.request("GET", "/probe")
                assert str(caught.value) == "CONTROL_HTTP_ERROR" and caught.value.broker_code is None
                assert stream.closed
    asyncio.run(scenario())
