"""Durable simulated Slurm backend. Uploaded source is never executed."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping, Sequence
from uuid import UUID

from .preparation import BackendError, canonical_json_bytes, connection_revision_sha256, sha256, verify_package


class FakeBackend:
    def __init__(self, connection: dict):
        self.connection = connection
        self.directory = Path(connection.get("state_directory", "/tmp/compute-relay-fake"))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, attempt_id: str) -> Path:
        return self.directory / (str(UUID(attempt_id)) + ".json")

    def _load(self, attempt_id: str) -> dict | None:
        path = self._path(attempt_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def _save(self, attempt_id: str, record: dict) -> None:
        with tempfile.NamedTemporaryFile(mode="wb", dir=self.directory, delete=False) as stream:
            name = stream.name
            stream.write(canonical_json_bytes(record))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, self._path(attempt_id))
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def submit(self, attempt_id: str, manifest: dict, files: Mapping[str, bytes]) -> dict:
        digest = sha256(canonical_json_bytes(manifest))
        verify_package(manifest, digest, files)
        if manifest["connection_revision_sha256"] != connection_revision_sha256(self.connection):
            raise BackendError("Fake connection revision differs from prepared package")
        with open(self.directory / (str(UUID(attempt_id)) + ".lock"), "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record = self._load(attempt_id)
            if record:
                if record["manifest_sha256"] != digest:
                    raise BackendError("Attempt already has a different immutable package")
                return {"outcome": "submitted", "native_identity": record["native_identity"], "simulation": True}
            identity = {"attempt_id": attempt_id, "job_id": str(int(UUID(attempt_id)) % 10**12), "cluster": "simulation"}
            record = {"native_identity": identity, "manifest_sha256": digest, "manifest": manifest,
                      "observations": 0, "cancelled": False}
            self._save(attempt_id, record)
            return {"outcome": "submitted", "native_identity": identity, "simulation": True}

    def reconcile(self, attempt_id: str, manifest: dict) -> dict:
        record = self._load(attempt_id)
        if not record:
            return {"outcome": "submission_uncertain", "simulation": True}
        if record["manifest_sha256"] != sha256(canonical_json_bytes(manifest)):
            raise BackendError("Fake reconciliation package differs")
        return {"outcome": "submitted", "native_identity": record["native_identity"], "simulation": True}

    def _owned(self, identity: dict) -> dict:
        record = self._load(identity["attempt_id"])
        if not record or record["native_identity"] != identity:
            raise BackendError("Unknown simulated native identity")
        return record

    def observe(self, native_identity: dict) -> dict:
        record = self._owned(native_identity)
        state = "cancelled" if record["cancelled"] else self.connection.get("fake_outcome", "completed")
        if state not in ("completed", "failed", "cancelled"):
            state = "running"
        terminal = state in ("completed", "failed", "cancelled")
        record["observations"] += 1
        self._save(native_identity["attempt_id"], record)
        resource = record["manifest"]["maximum_allocation"]
        seconds = min(resource["time_limit_seconds"], max(0, int(self.connection.get("fake_elapsed_seconds", 1))))
        if resource["cpu_cores"] > 0 or resource["gpu_devices"] > 0:
            seconds = max(1, seconds)
        return {"state": state, "native_state": state.upper(), "terminal": terminal, "simulation": True,
                "accounting": {"final": terminal, "cpu_core_seconds": resource["cpu_cores"] * seconds,
                               "gpu_device_seconds": resource["gpu_devices"] * seconds}}

    def cancel(self, native_identity: dict) -> dict:
        record = self._owned(native_identity)
        record["cancelled"] = True
        self._save(native_identity["attempt_id"], record)
        return {"outcome": "cancel_requested", "simulation": True}

    def collect(self, native_identity: dict, outputs: Sequence[str] = ()) -> dict:
        record = self._owned(native_identity)
        if set(outputs) - set(record["manifest"]["request"]["outputs"]):
            raise BackendError("Undeclared simulated output")
        metrics = canonical_json_bytes({"simulation": True, "attempt_id": native_identity["attempt_id"], "executed_uploaded_code": False})
        return {"stdout": "Simulated Slurm job; uploaded code was not executed.\n", "stderr": "",
                "artifacts": {path: metrics for path in outputs}, "simulation": True}

    def discover(self) -> dict:
        return {"simulation": True, "partitions": ["debug"], "authorization": "simulated only",
                "software": {"status": "simulated", "scope": "login_node", "verified_on_compute": False,
                    "modules": {"status": "simulated", "available": [], "truncated": False},
                    "conda": {"status": "not_configured", "prefixes": [], "truncated": False},
                    "python": [{"kind": "system", "interpreter": "/usr/bin/python3", "source": "simulation", "verified_on_compute": False}],
                    "environments": [], "search": {"visited_directories": 0, "truncated": False, "roots": []},
                    "notes": ["Simulated candidates; no software was inspected or executed."]}}
