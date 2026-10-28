"""Django-free, fixed read-only maintenance using protected SSH configuration."""

from datetime import datetime, timezone

from compute_relay.worker.enrollment import profile_metadata, resolve_connection
from .planning import roots_from_metadata, validate_observation
from .protocol import probe_context
from .remote import REMOTE_SOURCE


def unavailable(code):
    return {"schema_version": "1.0", "source": "statvfs", "status": "unavailable",
            "observed_at": datetime.now(timezone.utc).isoformat(), "filesystem_id": None,
            "identity_verified": False, "total_bytes": None, "available_bytes": None,
            "total_inodes": None, "available_inodes": None, "quota": {"status": "unknown"}, "error_code": code}


def _protected_probe(worker, lease, context):
    configuration, public, revision = resolve_connection(lease["configuration"], worker.credential_profiles)
    profile = worker.credential_profiles[public["credential_profile"]]
    metadata = profile_metadata(public["credential_profile"], profile)
    if revision != context["connection_revision_sha256"] or metadata["revision_sha256"] != context["profile_revision_sha256"]:
        raise ValueError("Protected binding changed")
    root = next((item for item in roots_from_metadata(metadata) if item["alias"] == context["root_alias"]), None)
    if root is None:
        raise ValueError("Protected root unavailable")
    # Bound one maintenance operation independently of a job's SSH timeout.
    configuration = {**configuration, "command_timeout_seconds": min(configuration.get("command_timeout_seconds", 30), 30)}
    return configuration, root


def _probe_request(context, root):
    return {"operation": "probe_storage", "context": context,
        "root": root["path"], "expected_filesystem_id": root["filesystem_id"]}


def _probe_observation(result, context, root):
    if not isinstance(result, dict) or result.get("context") != context or set(result) != {"context", "observation"}:
        raise ValueError("Unbound probe result")
    observation = validate_observation(result["observation"])
    if root["filesystem_id"] is None:
        observation["identity_verified"] = False
    elif observation["filesystem_id"] != root["filesystem_id"]:
        observation.update(identity_verified=False, status="unavailable", error_code="FILESYSTEM_IDENTITY_MISMATCH")
    return observation


def _report(lease, context, observation):
    return {"lease_id": lease["id"], "generation": lease["generation"], "target_id": context["target_id"],
        "profile_revision_sha256": context["profile_revision_sha256"],
        "connection_revision_sha256": context["connection_revision_sha256"], "observation": observation}


async def execute(worker, lease):
    context = probe_context(lease)
    try:
        configuration, root = _protected_probe(worker, lease, context)
        if configuration["backend"] == "fake":
            observation = unavailable("SIMULATED_STORAGE_NOT_PROBED")
        else:
            backend = worker.backend_factory(configuration)
            result = await backend.transport.call_helper(REMOTE_SOURCE, _probe_request(context, root))
            observation = _probe_observation(result, context, root)
    except Exception:
        observation = unavailable("STORAGE_PROBE_UNAVAILABLE")
    await worker.client.request("POST", f"/internal/v1/extensions/storage/pools/{context['pool_id']}/report", _report(lease, context, observation))
