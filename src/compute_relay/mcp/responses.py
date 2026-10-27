"""Small agent-facing job results; workload bytes require explicit reads."""

from copy import deepcopy


_USAGE = ("cpu_core_seconds", "gpu_device_seconds")
_LIMITS = ("nodes", "cpu_cores", "gpu_devices", "memory_mib", "time_limit_seconds")
_TERMINAL = {"succeeded", "failed", "cancelled", "expired"}


def _object(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _fields(value, names, *, text_limit=4000):
    """Select scalar fields, without echoing arbitrary remote nested objects."""
    result = {}
    for name in names:
        item = _object(value).get(name)
        if item is None or type(item) in {int, bool}:
            if name in _object(value):
                result[name] = item
        elif isinstance(item, str):
            result[name] = item[:text_limit]
    return result


def _approval(value):
    value = _object(value)
    if not value:
        return None
    return {"requires_human_approval": value.get("requires_human_approval") is True,
        "approval_reasons": [_fields(item, ("code", "source", "message"), text_limit=512)
            for item in _list(value.get("approval_reasons"))[:8] if isinstance(item, dict)]}


def _accounting(value):
    value = _object(value)
    result = _fields(value, ("final", *_USAGE, "elapsed_seconds"))
    for name, fields in (("requested", ("cpu_cores", "gpu_devices", "time_limit_seconds")),
        ("allocated", ("cpu_cores", "gpu_devices")), ("prediction", ("cpu_cores", "gpu_devices", "allocation_exceeded")),
        ("reservation", _USAGE), ("reservation_difference", _USAGE)):
        if isinstance(value.get(name), dict):
            result[name] = _fields(value[name], fields)
    return result


def compact_allowance(payload):
    """Refresh shared budget and admission counts without repeating discovery."""
    if not isinstance(payload, dict):
        raise ValueError("Expected a broker allowance object.")
    result = {"response_mode": "budget", **_fields(payload,
        ("project_id", "mode", "paused", "policy_revision", "admission_expires_at"))}
    for name in ("limit", "spent", "reserved", "reserved_awaiting_approval", "available", "debt"):
        if isinstance(payload.get(name), dict):
            result[name] = _fields(payload[name], _USAGE)
    if isinstance(payload.get("jobs"), dict):
        result["jobs"] = _fields(payload["jobs"],
            ("limit", "admitted", "remaining", "active", "active_limit", "awaiting_approval"))
    return deepcopy(result)


def compact_job(payload, *, broker_url=None):
    """Summarize an authorized REST job without another network operation.

    Full metadata remains available through ``get_job(detail=True)``. This
    deliberately makes no claim that declared artifacts exist or that scheduler
    completion establishes correctness of a scientific result.
    """
    if not isinstance(payload, dict):
        raise ValueError("Expected a broker job object.")
    result = {"response_mode": "compact", **_fields(payload, ("id", "name", "state", "manifest_sha256"))}
    manifest = _object(payload.get("manifest"))
    if isinstance(manifest.get("reservation"), dict):
        result["reservation"] = _fields(manifest["reservation"], _USAGE)
    validation = _object(payload.get("resource_validation"))
    if validation:
        limits = _object(validation.get("effective_limits"))
        result["resource_validation"] = {
            **_fields(validation, ("valid", "project_envelope_human_override_allowed")),
            "violations": [_fields(item, ("resource", "requested", "effective", "limit", "excess", "source", "unit"), text_limit=80)
                for item in _list(validation.get("violations"))[:8] if isinstance(item, dict)],
            "effective_limits": _fields(limits.get("per_job_limits"), _LIMITS),
        }
    executions = payload.get("execution_requests", [])
    latest_execution = _object(executions[-1]) if isinstance(executions, list) and executions else {}
    if latest_execution:
        result["latest_execution"] = _fields(latest_execution, ("id", "state", "comment"))
        if isinstance(latest_execution.get("budget_reservation"), dict):
            result["latest_execution"]["budget_reservation"] = _fields(
                latest_execution["budget_reservation"], ("state", *_USAGE))
    approval = _approval(latest_execution.get("approval") or payload.get("approval"))
    if approval is not None:
        result["approval"] = approval
    current_approval = _approval(latest_execution.get("approval_current"))
    if current_approval is not None and current_approval != approval:
        result["approval_current"] = current_approval
    attempts = [item for item in _list(payload.get("attempts")) if isinstance(item, dict)]
    if any(not isinstance(item.get("execution_request_id"), str) or not item["execution_request_id"] for item in attempts):
        raise ValueError("Broker attempts must identify their execution request.")
    latest_attempt = attempts[-1] if attempts else {}
    if latest_execution:
        latest_attempt = next((item for item in reversed(attempts)
            if item["execution_request_id"] == latest_execution.get("id")), {})
    previous_attempt_count = len(attempts) - bool(latest_attempt)
    if previous_attempt_count:
        result["previous_attempt_count"] = previous_attempt_count
    if latest_attempt:
        result["latest_attempt"] = _fields(latest_attempt,
            ("id", "execution_request_id", "state", "accounting_state", "collection_state", "error_code", "native_state", "exit_code"), text_limit=80)
        if isinstance(latest_attempt.get("native_identity"), dict):
            result["latest_attempt"]["native_identity"] = _fields(latest_attempt["native_identity"],
                ("job_id", "cluster"), text_limit=80)
        if isinstance(latest_attempt.get("accounting"), dict):
            result["latest_attempt"]["accounting"] = _accounting(latest_attempt["accounting"])
    job_id = result.get("id")
    if job_id:
        result["review_url"] = (broker_url.rstrip("/") if broker_url else "") + f"/review/{job_id}/"
    outputs = _object(payload.get("request")).get("outputs")
    result["result_availability"] = {"artifacts": "not_checked" if latest_attempt else "not_started",
        "logs": "available" if latest_attempt else "not_started"}
    if isinstance(outputs, list):
        result["result_availability"]["declared_output_count"] = len(outputs)
        if not outputs:
            result["result_availability"]["artifacts"] = "no_declared_outputs"
    if latest_attempt.get("collection_state"):
        result["result_availability"]["collection"] = latest_attempt["collection_state"]
    if latest_execution.get("state") == "awaiting_approval":
        result["next_action"] = {"action": "await_human_review"}
    elif latest_execution.get("state") == "rejected":
        result["next_action"] = {"action": "revise_request", "tool": "revise_job", "job_id": job_id}
    elif latest_execution.get("state") in {"cancelled", "expired"}:
        result["next_action"] = {"action": "none"}
    elif latest_attempt and latest_attempt.get("state") in _TERMINAL and latest_attempt.get("collection_state") == "retry_pending":
        result["next_action"] = {"action": "await_output_collection", "tool": "get_job", "job_id": job_id}
    elif latest_attempt and latest_attempt.get("state") in _TERMINAL and latest_attempt.get("collection_state") == "failed":
        result["next_action"] = {"action": "recover_outputs", "tool": "retry_job_outputs", "attempt_id": latest_attempt.get("id")}
    elif latest_attempt and latest_attempt.get("state") in _TERMINAL:
        result["next_action"] = {"action": "inspect_results", "tool": "list_job_artifacts" if outputs else "get_job_logs",
            "attempt_id": latest_attempt.get("id")}
    elif latest_attempt:
        result["next_action"] = {"action": "wait", "tool": "get_job", "job_id": job_id}
    elif validation.get("valid") is False and not validation.get("project_envelope_human_override_allowed"):
        result["next_action"] = {"action": "revise_request", "tool": "revise_job", "job_id": job_id}
    else:
        result["next_action"] = {"action": "request_execution", "tool": "request_execution", "job_id": job_id}
    return deepcopy(result)
