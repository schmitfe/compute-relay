"""Typed, transport-independent rules for execution workers and the controller.

The I/O adapters own awaiting and journaling. Package integrity, submission
decisions, cancellation semantics and replay binding have one definition here.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID
from compute_relay.transfer import FileReference, INLINE_BYTES, MAX_FILE_BYTES


Operation = Literal["submit", "reconcile", "observe", "cancel", "cleanup"]


class CancellationStatus(StrEnum):
    ACCEPTED = "cancel_requested"
    UNKNOWN = "unknown"
    ALREADY_TERMINAL = "already_terminal"
    IDENTITY_MISMATCH = "identity_mismatch"


REPORT_OUTCOMES = {
    "submit": frozenset({"submitted", "submission_uncertain", "rejected"}),
    "reconcile": frozenset({"submitted", "submission_uncertain", "rejected"}),
    "observe": frozenset({"observed"}),
    "cancel": frozenset({"cancel_requested", "cancel_unknown", "cancel_identity_mismatch", "observed"}),
    "cleanup": frozenset({"cleaned", "cleanup_failed"}),
}
TERMINAL_OBSERVATION_STATES = frozenset({"completed", "succeeded", "failed", "cancelled", "expired"})


def validate_report_outcome(operation, outcome):
    if not isinstance(operation, str) or not isinstance(outcome, str) or outcome not in REPORT_OUTCOMES.get(operation, ()):
        raise ValueError("Result does not match the leased operation")


@dataclass(frozen=True)
class PreparedWork:
    operation: Operation
    attempt_id: str
    connection_id: str
    manifest: dict
    manifest_sha256: str
    files: dict[str, bytes | FileReference]

    @classmethod
    def decode(cls, lease):
        operation = lease.get("operation")
        if operation not in REPORT_OUTCOMES:
            raise ValueError("Unsupported worker operation")
        attempt_id = str(UUID(lease["attempt_id"]))
        connection_id = str(UUID(lease["connection_id"]))
        package = lease["prepared"]
        manifest = package["manifest"]
        if manifest["request"]["connection_id"] != connection_id:
            raise ValueError("Package connection mismatch")
        files, size = {}, 0
        if not isinstance(package["files"], dict) or len(package["files"]) > 110:
            raise ValueError("Package exceeds worker file limit")
        for path, encoded in package["files"].items():
            if isinstance(encoded, dict):
                if set(encoded) != {"sha256", "size_bytes"} or type(encoded["size_bytes"]) is not int or not INLINE_BYTES < encoded["size_bytes"] <= MAX_FILE_BYTES:
                    raise ValueError("Invalid streamed input descriptor")
                files[path] = FileReference(**encoded)
                continue
            if not isinstance(encoded, str) or len(encoded) > 140 * 1024 * 1024:
                raise ValueError("File exceeds worker limit")
            content = base64.b64decode(encoded, validate=True)
            size += len(content)
            if size > 256 * 1024 * 1024 or len(files) >= 110:
                raise ValueError("Package exceeds worker limit")
            files[path] = content
        return cls(operation, attempt_id, connection_id, manifest, package["manifest_sha256"], files)

    def verify(self, connection, previous, extensions):
        from compute_relay.backends import connection_revision_sha256, verify_package
        if previous and previous["manifest_sha256"] != self.manifest_sha256:
            raise ValueError("Attempt package changed after intent")
        # A submit lease recovered after an existing intent is management work;
        # an upgrade must never turn it into a second submission.
        operation = "reconcile" if self.operation == "submit" and previous else self.operation
        verify_package(self.manifest, self.manifest_sha256, self.files, operation=operation)
        if connection_revision_sha256(connection) != self.manifest["connection_revision_sha256"]:
            raise ValueError("Configured connection revision changed")
        name = self.manifest.get("extension_name")
        if self.operation == "submit" and not previous and name and name not in extensions:
            raise ValueError("This extension package is not enabled on the worker")


def submission_plan(operation, previous):
    if previous and previous.get("outcome") in {"submitted", "rejected"}:
        return "replay", previous["result"]
    return ("reconcile", None) if previous or operation == "reconcile" else ("submit", None)


def submission_record(manifest_sha256, result):
    if not isinstance(result, dict) or result.get("outcome") not in {"submitted", "submission_uncertain", "rejected"}:
        result = {"outcome": "submission_uncertain"}
    result = {key: value for key, value in result.items() if key in {"outcome", "native_identity"}}
    if result.get("outcome") == "submitted" and not isinstance(result.get("native_identity"), dict):
        result = {"outcome": "submission_uncertain"}
    record = {"manifest_sha256": manifest_sha256, "outcome": result["outcome"], "result": result}
    if result.get("native_identity"):
        record["native_identity"] = result["native_identity"]
    return record


def native_for(lease, previous):
    leased = lease.get("native_identity")
    recorded = (previous or {}).get("native_identity")
    if leased and recorded and leased != recorded:
        raise ValueError("Native identity changed after the durable submission receipt")
    native = leased or recorded
    if native is not None and not isinstance(native, dict):
        raise ValueError("Native identity is not an object")
    if native and native.get("attempt_id") is not None and native["attempt_id"] != lease["attempt_id"]:
        raise ValueError("Native identity belongs to another attempt")
    return native


def terminal_observation(observation):
    return (isinstance(observation, dict) and observation.get("terminal") is True
        and observation.get("state") in TERMINAL_OBSERVATION_STATES)


def cancellation_status(reply):
    try:
        return CancellationStatus(reply.get("outcome")) if isinstance(reply, dict) else CancellationStatus.UNKNOWN
    except ValueError:
        return CancellationStatus.UNKNOWN


def cancellation_result(reply, native, observation=None):
    status = cancellation_status(reply)
    if terminal_observation(observation):
        return {"outcome": "observed", "native_identity": native, "observation": observation}
    if status == CancellationStatus.ACCEPTED:
        return {"outcome": "cancel_requested", "native_identity": native}
    mismatch = status == CancellationStatus.IDENTITY_MISMATCH
    result = {"outcome": "cancel_identity_mismatch" if mismatch else "cancel_unknown", "native_identity": native,
        "error_code": "CANCEL_IDENTITY_MISMATCH" if mismatch else "CANCEL_UNCONFIRMED"}
    if isinstance(observation, dict):
        result["observation"] = observation
    return result


def report_payload(lease, result):
    validate_report_outcome(lease["operation"], result["outcome"])
    return {"lease_id": lease["id"], "generation": lease["generation"], **result}


def report_record(lease, manifest_sha256, result, *, phase="ready"):
    return {"schema_version": "1.0", "attempt_id": lease["attempt_id"], "manifest_sha256": manifest_sha256,
        "operation": lease["operation"], "extension_context": deepcopy(lease.get("extension_context", {})),
        "phase": phase, "report": report_payload(lease, result)}


def replay_result(record, lease, manifest_sha256, native):
    """Rebind durable results only to equivalent current management authority."""
    if not record:
        return None
    if record.get("attempt_id") != lease["attempt_id"] or record.get("manifest_sha256") != manifest_sha256:
        raise ValueError("Saved report belongs to another attempt package")
    saved = record["report"]
    if native and saved.get("native_identity") and saved["native_identity"] != native:
        raise ValueError("Native identity changed after the durable report")
    same_lease = saved["lease_id"] == lease["id"] and saved["generation"] == lease["generation"]
    if lease.get("collection_only") and not same_lease:
        return None
    result = {key: deepcopy(value) for key, value in saved.items() if key not in {"lease_id", "generation"}}
    if same_lease:
        validate_report_outcome(lease["operation"], result["outcome"])
        return result
    if result.get("outcome") in {"submitted", "rejected"} and lease["operation"] == "reconcile":
        return result
    if (lease["operation"] not in {"observe", "cancel"} or result.get("outcome") != "observed"
            or not terminal_observation(result.get("observation")) or result.get("native_identity") != native):
        return None
    old_contexts, new_contexts = record.get("extension_context", {}), lease.get("extension_context", {})
    for name, metadata in list(result.get("extension_data", {}).items()):
        old = {key: value for key, value in old_contexts.get(name, {}).items() if key != "generation"}
        new = {key: value for key, value in new_contexts.get(name, {}).items() if key != "generation"}
        if old != new:
            # Core accounting remains authoritative even when an extension's
            # context has changed. Its old metadata cannot acquire new authority.
            del result["extension_data"][name]
            continue
        if "generation" in metadata:
            metadata["generation"] = lease["generation"]
    return result
