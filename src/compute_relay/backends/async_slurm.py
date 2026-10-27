"""Awaitable Slurm operations using the same fixed helper and parsing rules."""

from __future__ import annotations

import base64
from typing import Mapping, Sequence
from uuid import UUID
from compute_relay.transfer import FileReference

from compute_relay.transports.async_openssh import AsyncOpenSSHTransport
from compute_relay.transports.openssh import TransportError

from .discovery import parse_discovery
from .preparation import canonical_json_bytes, helper_source, relative_path, sha256, verify_package
from .slurm import SlurmBackend, parse_observation


class AsyncSlurmBackend(SlurmBackend):
    def __init__(self, connection: dict, transport=None):
        super().__init__(connection, transport=transport or AsyncOpenSSHTransport(connection))

    async def _call(self, operation: str, **arguments) -> dict:
        return await self.transport.call_helper(helper_source(operation), self._request(operation, **arguments))

    async def submit(self, attempt_id: str, manifest: dict, files: Mapping[str, bytes | FileReference]) -> dict:
        UUID(attempt_id)
        self._verify_binding(manifest)
        verify_package(manifest, sha256(canonical_json_bytes(manifest)), files)
        try:
            response = await self._call("submit", attempt_id=attempt_id, manifest=manifest,
                files={path: None if isinstance(data, FileReference) else base64.b64encode(data).decode("ascii") for path, data in files.items()})
            return self._submission_result(response)
        except TransportError:
            return {"outcome": "submission_uncertain", "detail": "Transport failed; reconcile the existing attempt"}

    async def reconcile(self, attempt_id: str, manifest: dict) -> dict:
        UUID(attempt_id)
        self._verify_binding(manifest)
        try:
            return self._submission_result(await self._call("reconcile", attempt_id=attempt_id, manifest=manifest, **self._management(manifest)))
        except TransportError:
            return {"outcome": "submission_uncertain", "detail": "Reconciliation unavailable"}

    async def observe(self, native_identity: dict, *, manifest: dict) -> dict:
        try:
            response = await self._call("observe", native_identity=native_identity, **self._management(manifest))
            return parse_observation(native_identity, response["queue"], response["accounting"], response.get("expected_gpus", 0))
        except (TransportError, KeyError):
            return {"state": "unknown", "native_state": "UNKNOWN", "terminal": False, "accounting": {"final": False}}

    async def observe_many(self, items):
        if not 1 <= len(items) <= 64:
            raise ValueError("Invalid status batch size")
        requests = [{"native_identity": item["native_identity"], **self._management(item["manifest"])} for item in items]
        unknown = {"state": "unknown", "native_state": "UNKNOWN", "terminal": False, "accounting": {"final": False}}
        try:
            response = await self._call("observe_many", items=requests)
            values = response["items"]
            if not isinstance(values, dict):
                raise ValueError("Invalid batch status response")
        except (TransportError, KeyError, ValueError, TypeError):
            values = {}
        results = {}
        for item in items:
            native = item["native_identity"]
            value = values.get(native["attempt_id"], {})
            try:
                results[native["attempt_id"]] = parse_observation(native, value["queue"], value["accounting"], value.get("expected_gpus", 0))
            except (KeyError, ValueError, TypeError, AttributeError):
                results[native["attempt_id"]] = dict(unknown)
        return results

    async def cancel(self, native_identity: dict, *, manifest: dict) -> dict:
        try:
            result = await self._call("cancel", native_identity=native_identity, **self._management(manifest))
            outcome = result.get("outcome") if isinstance(result, dict) else None
            return {"outcome": outcome if outcome in {"cancel_requested", "already_terminal", "identity_mismatch", "unknown"} else "unknown"}
        except TransportError:
            return {"outcome": "unknown"}

    async def collect(self, native_identity: dict, outputs: Sequence[str] = (), *, manifest: dict) -> dict:
        for path in outputs:
            relative_path(path)
        response = await self._call("collect", native_identity=native_identity, outputs=list(outputs), **self._management(manifest))
        artifacts = {path: base64.b64decode(data, validate=True) for path, data in response.pop("artifacts_base64", {}).items()}
        return {**response, "artifacts": artifacts}

    async def stage_input(self, attempt_id, manifest, path, **chunk):
        self._verify_binding(manifest)
        return await self._call("stage_input", attempt_id=attempt_id, manifest=manifest, path=path, **chunk)

    async def read_logs(self, native_identity, *, manifest, cursors):
        return await self._call("logs", native_identity=native_identity, cursors=cursors, **self._management(manifest))

    async def output_manifest(self, native_identity, *, manifest):
        return await self._call("output_manifest", native_identity=native_identity, **self._management(manifest))

    async def output_chunk(self, native_identity, *, manifest, path, offset, version):
        return await self._call("output_chunk", native_identity=native_identity, path=path, offset=offset,
            version=version, **self._management(manifest))

    async def discover(self) -> dict:
        return parse_discovery(await self._call("discover"), self.connection)
