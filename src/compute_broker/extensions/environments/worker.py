"""Optional worker hook; only the fixed metadata source may run on the login node."""

from uuid import UUID

from .remote import REMOTE_SOURCE


def after_observation(backend, native, manifest, context):
    generation = context.get("generation")
    unverified = {"verified": False, "error_code": "ENVIRONMENT_METADATA_UNVERIFIED"}
    if getattr(backend, "connection", {}).get("backend") == "fake":
        return {"environment_finalization": {"verified": False, "error_code": "SIMULATED_BUILD_NOT_PUBLISHED"}, "generation": generation}
    try:
        build_id = str(UUID(context["build_id"]))
        if str(UUID(native["attempt_id"])) != context["attempt_id"] or manifest.get("extension_name") != "environments":
            raise ValueError("extension identity mismatch")
        result = backend.transport.call_helper(REMOTE_SOURCE, {"operation": "finalize_environment",
            "config": {"remote_root": backend.connection["remote_root"]}, "native_identity": native,
            "build_id": build_id, "spec_sha256": context["spec_sha256"]})
    except Exception:
        result = unverified
    return {"environment_finalization": result, "generation": generation}
