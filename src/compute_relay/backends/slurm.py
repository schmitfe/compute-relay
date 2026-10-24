"""Slurm observation parsing and protected remote-helper adapter."""

from __future__ import annotations

import base64
import datetime as dt
import re
from typing import Mapping, Sequence
from uuid import UUID

from compute_relay.transports import OpenSSHTransport, TransportError

from .discovery import parse_discovery
from .preparation import (
    BackendError, canonical_json_bytes, connection_revision_sha256, helper_source,
    relative_path, sha256, verify_package,
)


_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "REVOKED"}


def normalize_state(native_state: str) -> str:
    state = native_state.split("+", 1)[0].split()[0] if native_state.strip() else "UNKNOWN"
    if state in ("PENDING", "CONFIGURING", "REQUEUED", "REQUEUE_HOLD", "RESV_DEL_HOLD"):
        return "queued"
    if state in ("RUNNING", "COMPLETING", "SUSPENDED", "STAGE_OUT", "STOPPED"):
        return "running"
    if state == "COMPLETED":
        return "completed"
    if state == "CANCELLED":
        return "cancelled"
    if state in _TERMINAL:
        return "failed"
    return "unknown"


def _gpu_count(tres: str) -> int | None:
    values = {}
    for field in tres.split(","):
        key, sep, value = field.partition("=")
        if sep and (key == "gres/gpu" or key.startswith("gres/gpu:")):
            if not value.isdigit() or key in values:
                return None
            values[key] = int(value)
    if "gres/gpu" in values:
        typed = sum(value for key, value in values.items() if key != "gres/gpu")
        if typed and typed != values["gres/gpu"]:
            return None
        return values["gres/gpu"]
    return sum(values.values()) if values else None


def submission_time_matches(value: str, native_identity: dict) -> bool:
    try:
        submitted = dt.datetime.fromisoformat(value)
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=dt.timezone.utc)
        ours = dt.datetime.fromisoformat(native_identity["submitted_at"])
        return ours - dt.timedelta(seconds=60) <= submitted <= ours + dt.timedelta(seconds=120)
    except (ValueError, KeyError, TypeError):
        return False


def parse_observation(native_identity: dict, queue: dict, accounting: dict, expected_gpus: int = 0) -> dict:
    """Keep missing/lagging accounting uncertain; never invent a zero charge."""
    job_id = native_identity["job_id"]
    marker = "broker-attempt:" + native_identity.get("attempt_id", "")
    unknown = {"state": "unknown", "native_state": "UNKNOWN", "terminal": False,
               "accounting": {"final": False}, "source": "none"}
    if queue.get("returncode") == 0:
        for line in queue.get("stdout", "").splitlines():
            fields = [field.strip() for field in line.strip().split("|")]
            if fields and fields[0] == job_id:
                if len(fields) != 4 or fields[2] != marker or not submission_time_matches(fields[3], native_identity):
                    return {**unknown, "detail": "Live native job identity no longer matches the broker attempt"}
                state = normalize_state(fields[1])
                # Even terminal-looking queue rows wait for final accounting.
                return {"state": state, "native_state": fields[1], "terminal": False,
                        "accounting": {"final": False}, "source": "squeue"}
    if accounting.get("returncode") != 0:
        return unknown
    rows = [[field.strip() for field in line.split("|")] for line in accounting.get("stdout", "").splitlines() if line.strip()]
    rows = [row for row in rows if len(row) >= 11 and row[0] == job_id]
    if len(rows) != 1:
        return {**unknown, "detail": "Missing or multiple allocation records; settlement deferred"}
    row = rows[0]
    if not submission_time_matches(row[6], native_identity):
        return {**unknown, "detail": "Accounting Submit is outside the original submission window"}
    comment_matches = row[9] == marker
    work_dir_matches = bool(native_identity.get("work_dir")) and row[10] == native_identity["work_dir"]
    if (row[9] and not comment_matches) or not (comment_matches or work_dir_matches):
        return {**unknown, "detail": "Accounting record does not identify the original broker attempt"}
    native_state = row[1].split("+", 1)[0].split()[0] if row[1].strip() else "UNKNOWN"
    state = normalize_state(native_state)
    observation = {"state": state, "native_state": native_state, "terminal": native_state in _TERMINAL,
                   "accounting": {"final": False}, "exit_code": row[5], "source": "sacct"}
    if not observation["terminal"]:
        return observation
    try:
        elapsed = int(row[2])
        cpus = int(row[3])
        if elapsed < 0 or cpus < 0:
            return observation
        dt.datetime.fromisoformat(row[8])  # End must be a real terminal timestamp.
        if not re.fullmatch(r"[0-9]+:[0-9]+", row[5]):
            return observation
        gpu_count = _gpu_count(row[4])
        if gpu_count is None:
            if "gres/gpu" in row[4] or (expected_gpus and not (elapsed == 0 and cpus == 0)):
                return observation
            gpu_count = 0
        # Slurm reports whole seconds. A real allocation that finishes within
        # its first reported second must still consume finite project allowance.
        metered_seconds = max(1, elapsed) if cpus > 0 or gpu_count > 0 else elapsed
        observation["accounting"] = {"final": True, "cpu_core_seconds": cpus * metered_seconds,
                                     "gpu_device_seconds": gpu_count * metered_seconds}
        observation["allocated_cpus"] = cpus
        observation["allocated_gpus"] = gpu_count
        observation["elapsed_seconds"] = metered_seconds
        observation["scheduler_elapsed_seconds"] = elapsed
        return observation
    except (ValueError, KeyError, TypeError, OverflowError):
        return observation


class SlurmBackend:
    def __init__(self, connection: dict, transport=None):
        self.connection = connection
        self.transport = transport or OpenSSHTransport(connection)

    def _request(self, operation: str, **arguments) -> dict:
        # Only fixed operation names exist. No shell command, host, or credential
        # is accepted from an agent request.
        remote_config = {key: self.connection[key] for key in
                         ("remote_root", "slurm_paths", "scheduler_environment") if key in self.connection}
        if operation == "discover" and "environment_discovery" in self.connection:
            remote_config["environment_discovery"] = self.connection["environment_discovery"]
        return {"operation": operation, "config": remote_config, **arguments}

    def _call(self, operation: str, **arguments) -> dict:
        return self.transport.call_helper(helper_source(), self._request(operation, **arguments))

    def _verify_binding(self, manifest: dict) -> None:
        if manifest["connection_revision_sha256"] != connection_revision_sha256(self.connection):
            raise BackendError("Protected connection revision differs from prepared package")

    @staticmethod
    def _submission_result(result: dict) -> dict:
        output = {"outcome": result.get("outcome", "submission_uncertain")}
        for key in ("native_identity", "detail"):
            if key in result:
                output[key] = result[key]
        return output

    def submit(self, attempt_id: str, manifest: dict, files: Mapping[str, bytes]) -> dict:
        UUID(attempt_id)
        self._verify_binding(manifest)
        verify_package(manifest, sha256(canonical_json_bytes(manifest)), files)
        try:
            response = self._call("submit", attempt_id=attempt_id, manifest=manifest,
                                  files={path: base64.b64encode(data).decode("ascii") for path, data in files.items()})
            return self._submission_result(response)
        except TransportError:
            return {"outcome": "submission_uncertain", "detail": "Transport failed; reconcile the existing attempt"}

    def reconcile(self, attempt_id: str, manifest: dict) -> dict:
        UUID(attempt_id)
        self._verify_binding(manifest)
        try:
            return self._submission_result(self._call("reconcile", attempt_id=attempt_id, manifest=manifest))
        except TransportError:
            return {"outcome": "submission_uncertain", "detail": "Reconciliation unavailable"}

    def observe(self, native_identity: dict) -> dict:
        try:
            response = self._call("observe", native_identity=native_identity)
            return parse_observation(native_identity, response["queue"], response["accounting"], response.get("expected_gpus", 0))
        except (TransportError, KeyError):
            return {"state": "unknown", "native_state": "UNKNOWN", "terminal": False, "accounting": {"final": False}}

    def cancel(self, native_identity: dict) -> dict:
        try:
            return self._call("cancel", native_identity=native_identity)
        except TransportError:
            return {"outcome": "unknown"}

    def collect(self, native_identity: dict, outputs: Sequence[str] = ()) -> dict:
        for path in outputs:
            relative_path(path)
        response = self._call("collect", native_identity=native_identity, outputs=list(outputs))
        artifacts = {path: base64.b64decode(data, validate=True) for path, data in response.pop("artifacts_base64", {}).items()}
        return {**response, "artifacts": artifacts}

    def discover(self) -> dict:
        """Read-only scheduler discovery; never allocates compute resources."""
        response = self._call("discover")
        return parse_discovery(response, self.connection)
