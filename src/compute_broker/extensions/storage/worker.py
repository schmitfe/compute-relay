"""Django-free, fixed read-only maintenance using protected SSH configuration."""

from datetime import datetime, timezone

from compute_broker.worker.enrollment import profile_metadata, resolve_connection
from .planning import roots_from_metadata, validate_observation
from .protocol import probe_context
from .remote import REMOTE_SOURCE


def unavailable(code):
    return {"schema_version": "1.0", "source": "statvfs", "status": "unavailable",
            "observed_at": datetime.now(timezone.utc).isoformat(), "filesystem_id": None,
            "identity_verified": False, "total_bytes": None, "available_bytes": None,
            "total_inodes": None, "available_inodes": None, "quota": {"status": "unknown"}, "error_code": code}


def execute(worker, lease):
    context = probe_context(lease)
    try:
        configuration, public, revision = resolve_connection(lease["configuration"], worker.credential_profiles)
        profile = worker.credential_profiles[public["credential_profile"]]
        metadata = profile_metadata(public["credential_profile"], profile)
        if revision != context["connection_revision_sha256"] or metadata["revision_sha256"] != context["profile_revision_sha256"]:
            raise ValueError("Protected binding changed")
        root = next((item for item in roots_from_metadata(metadata) if item["alias"] == context["root_alias"]), None)
        if root is None:
            raise ValueError("Protected root unavailable")
        if configuration["backend"] == "fake":
            observation = unavailable("SIMULATED_STORAGE_NOT_PROBED")
        else:
            # This isolated helper is short-lived; it cannot consume a job's
            # full SSH timeout while holding the maintenance lane indefinitely.
            configuration = {**configuration, "command_timeout_seconds": min(configuration.get("command_timeout_seconds", 30), 30)}
            backend = worker.backend_factory(configuration)
            result = backend.transport.call_helper(REMOTE_SOURCE, {"operation": "probe_storage", "context": context,
                "root": root["path"], "expected_filesystem_id": root["filesystem_id"]})
            if not isinstance(result, dict) or result.get("context") != context or set(result) != {"context", "observation"}:
                raise ValueError("Unbound probe result")
            observation = validate_observation(result["observation"])
            if root["filesystem_id"] is None:
                observation["identity_verified"] = False
            elif observation["filesystem_id"] != root["filesystem_id"]:
                observation.update(identity_verified=False, status="unavailable", error_code="FILESYSTEM_IDENTITY_MISMATCH")
    except Exception:
        observation = unavailable("STORAGE_PROBE_UNAVAILABLE")
    report = {"lease_id": lease["id"], "generation": lease["generation"], "target_id": context["target_id"],
              "profile_revision_sha256": context["profile_revision_sha256"],
              "connection_revision_sha256": context["connection_revision_sha256"], "observation": observation}
    worker.client.request("POST", f"/internal/v1/extensions/storage/pools/{context['pool_id']}/report", report)
