"""Awaitable Slurm operations using the same fixed helper and parsing rules."""

from __future__ import annotations

import base64
from typing import Mapping, Sequence
from uuid import UUID

from compute_broker.transports.async_openssh import AsyncOpenSSHTransport
from compute_broker.transports.openssh import TransportError

from .discovery import parse_discovery
from .preparation import canonical_json_bytes, helper_source, relative_path, sha256, verify_package
from .slurm import SlurmBackend, parse_observation


class AsyncSlurmBackend(SlurmBackend):
    def __init__(self, connection: dict, transport=None):
        super().__init__(connection, transport=transport or AsyncOpenSSHTransport(connection))

    async def _call(self, operation: str, **arguments) -> dict:
        return await self.transport.call_helper(helper_source(), self._request(operation, **arguments))

    async def submit(self, attempt_id: str, manifest: dict, files: Mapping[str, bytes]) -> dict:
        UUID(attempt_id)
        self._verify_binding(manifest)
        verify_package(manifest, sha256(canonical_json_bytes(manifest)), files)
        try:
            response = await self._call("submit", attempt_id=attempt_id, manifest=manifest,
                files={path: base64.b64encode(data).decode("ascii") for path, data in files.items()})
            return self._submission_result(response)
        except TransportError:
            return {"outcome": "submission_uncertain", "detail": "Transport failed; reconcile the existing attempt"}

    async def reconcile(self, attempt_id: str, manifest: dict) -> dict:
        UUID(attempt_id)
        self._verify_binding(manifest)
        try:
            return self._submission_result(await self._call("reconcile", attempt_id=attempt_id, manifest=manifest))
        except TransportError:
            return {"outcome": "submission_uncertain", "detail": "Reconciliation unavailable"}

    async def observe(self, native_identity: dict) -> dict:
        try:
            response = await self._call("observe", native_identity=native_identity)
            return parse_observation(native_identity, response["queue"], response["accounting"], response.get("expected_gpus", 0))
        except (TransportError, KeyError):
            return {"state": "unknown", "native_state": "UNKNOWN", "terminal": False, "accounting": {"final": False}}

    async def cancel(self, native_identity: dict) -> dict:
        try:
            return await self._call("cancel", native_identity=native_identity)
        except TransportError:
            return {"outcome": "unknown"}

    async def collect(self, native_identity: dict, outputs: Sequence[str] = ()) -> dict:
        for path in outputs:
            relative_path(path)
        response = await self._call("collect", native_identity=native_identity, outputs=list(outputs))
        artifacts = {path: base64.b64decode(data, validate=True) for path, data in response.pop("artifacts_base64", {}).items()}
        return {**response, "artifacts": artifacts}

    async def discover(self) -> dict:
        return parse_discovery(await self._call("discover"), self.connection)
