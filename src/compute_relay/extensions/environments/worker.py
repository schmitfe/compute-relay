"""Optional worker hook; only the fixed metadata source may run on the login node."""

from uuid import UUID

from .remote import REMOTE_SOURCE


def _finalization_request(backend, native, manifest, context):
    build_id = str(UUID(context["build_id"]))
    if str(UUID(native["attempt_id"])) != context["attempt_id"] or manifest.get("extension_name") != "environments":
        raise ValueError("extension identity mismatch")
    return {"operation": "finalize_environment",
        "config": {"remote_root": backend.connection["remote_root"]}, "native_identity": native,
        "build_id": build_id, "spec_sha256": context["spec_sha256"]}


def _result(context, result):
    return {"environment_finalization": result, "generation": context.get("generation")}


def _unverified():
    return {"verified": False, "error_code": "ENVIRONMENT_METADATA_UNVERIFIED"}


async def after_observation(backend, native, manifest, context):
    if context.get("inspection_id"):
        return {"inventory_collection": True, "generation": context.get("generation")}
    if getattr(backend, "connection", {}).get("backend") == "fake":
        return _result(context, {"verified": False, "error_code": "SIMULATED_BUILD_NOT_PUBLISHED"})
    try:
        result = await backend.transport.call_helper(REMOTE_SOURCE, _finalization_request(backend, native, manifest, context))
    except Exception:
        result = _unverified()
    return _result(context, result)
