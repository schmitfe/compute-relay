"""Pure compatibility rules for the fixed Slurm journal-management protocol.

The original unversioned slurm-ssh/v1 manifest is deliberately supported for
management. It has the same immutable package and journal identity format. This
does not authorize submitting it under a different execution-helper hash.
"""

import hashlib
import json

JOB_PROTOCOL = "slurm-jobs/v1"
MANAGEMENT_OPERATIONS = frozenset({"observe", "cancel", "reconcile", "collect", "logs", "output_manifest", "output_chunk"})
JOB_OPERATIONS = MANAGEMENT_OPERATIONS | {"submit", "stage_input"}


def is_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def management_protocol_supported(manifest):
    return (isinstance(manifest, dict)
            and manifest.get("schema_version") == "1.0"
            and manifest.get("adapter_version") == "slurm-ssh/v1"
            and manifest.get("helper_protocol", JOB_PROTOCOL) == JOB_PROTOCOL
            and is_digest(manifest.get("helper_sha256"))
            and is_digest(manifest.get("connection_revision_sha256"))
            and manifest.get("batch_script") == "_broker/batch.sh"
            and manifest.get("environment_wrapper") == "_broker/environment.sh")


def require_management_protocol(manifest):
    if not management_protocol_supported(manifest):
        raise ValueError("Unsupported prepared job management protocol")


def require_journal_binding(record, expected_manifest_sha256):
    if not is_digest(expected_manifest_sha256):
        raise ValueError("A prepared manifest digest is required")
    manifest_bytes = json.dumps(record.get("manifest") if isinstance(record, dict) else None,
                                sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if (not isinstance(record, dict)
            or record.get("manifest_sha256") != expected_manifest_sha256
            or hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha256):
        raise ValueError("Durable journal manifest differs from approved package")
    require_management_protocol(record["manifest"])
