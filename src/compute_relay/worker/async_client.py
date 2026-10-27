"""Async control HTTP with bounded streaming, no redirects and no retries."""

from __future__ import annotations

import asyncio
import json
import re

import httpx

from .client import ControlError, JsonClient, strict_json, _http_error, _MAX_ERROR_BYTES


class AsyncJsonClient(JsonClient):
    def __init__(self, *args, http_client=None, max_connections=8, **kwargs):
        super().__init__(*args, **kwargs)
        if type(max_connections) is not int or not 1 <= max_connections <= 64:
            raise ValueError("async HTTP concurrency must be between one and 64")
        if not 0 < self.timeout <= 600:
            raise ValueError("async HTTP timeout must be positive and at most 600 seconds")
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=self.timeout,
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
        )

    async def close(self):
        if self._owns_client:
            await self._http.aclose()

    async def _request(self, method, path, body=None, *, idempotency_key=None, limit=None):
        if not path.startswith("/") or path.startswith("//") or "\r" in path or "\n" in path:
            raise ValueError("request path must be origin-relative")
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json", "Accept-Encoding": "identity"}
        if idempotency_key:
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", idempotency_key):
                raise ValueError("invalid idempotency key")
            headers["Idempotency-Key"] = idempotency_key
        data = None
        if body is not None:
            data = json.dumps(body, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(data) > 16 * 1024 * 1024:
                raise ControlError("REQUEST_TOO_LARGE")
            headers["Content-Type"] = "application/json"
        maximum = self.max_response_bytes if limit is None else limit
        if type(maximum) is not int or maximum < 0:
            raise ValueError("invalid response bound")
        content = bytearray()
        try:
            async with asyncio.timeout(self.timeout):
                async with self._http.stream(method, self.base_url + path, content=data, headers=headers,
                                             follow_redirects=False) as response:
                    error = not 200 <= response.status_code < 300
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise ControlError("CONTROL_HTTP_ERROR", response.status_code) if error else ControlError("INVALID_CONTROL_RESPONSE")
                    bound = min(maximum, _MAX_ERROR_BYTES) if error else maximum
                    async for chunk in response.aiter_raw(chunk_size=65536):
                        if len(content) + len(chunk) > bound:
                            raise ControlError("CONTROL_HTTP_ERROR", response.status_code) if error else ControlError("RESPONSE_TOO_LARGE")
                        content.extend(chunk)
                    if error:
                        raise _http_error(response.status_code, bytes(content))
                    return bytes(content), response.headers.get("Content-Type", "application/octet-stream")
        except (httpx.HTTPError, TimeoutError, OSError):
            raise ControlError("CONTROL_UNAVAILABLE") from None

    async def request(self, method, path, body=None, *, idempotency_key=None):
        content, _ = await self._request(method, path, body, idempotency_key=idempotency_key)
        try:
            return strict_json(content) if content else {}
        except (ValueError, UnicodeError, RecursionError):
            raise ControlError("INVALID_CONTROL_RESPONSE") from None

    async def get_bytes(self, path: str, limit: int):
        return await self._request("GET", path, limit=limit)
