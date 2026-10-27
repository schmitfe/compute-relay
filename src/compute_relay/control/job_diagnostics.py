"""Bounded human diagnostics from an already-authorized Attempt.

This formatter grants no access or compute operation. Call it only after the
normal human/project authorization check. It deliberately excludes raw native
identity, observation details, request/configuration and exception strings.
"""

import re
from uuid import UUID


MAX_LOG_BYTES = 16384
_SAFE_INT = 2**53 - 1
_STATES = {"authorized", "queued", "running", "submission_uncertain", "cancel_requested", "cancel_pending", "succeeded", "failed", "cancelled", "expired", "unknown"}
_NATIVE_STATES = {"PENDING", "CONFIGURING", "REQUEUED", "REQUEUE_HOLD", "RESV_DEL_HOLD", "RUNNING", "COMPLETING",
    "SUSPENDED", "STAGE_OUT", "STOPPED", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
    "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "REVOKED", "PREEMPTED", "UNKNOWN"}
_ACCOUNTING = {"reserved", "settlement_pending", "settled", "released"}
_MODULE_INITIALIZER = re.compile(
    r"(?m)^(?:\./)?_broker/environment\.sh: line [0-9]+: "
    r"/(?:etc/profile\.d/(?:modules|lmod|z00_lmod)\.sh|usr/share/(?:Modules|modules)/init/bash|"
    r"usr/share/lmod/(?:lmod|[0-9][A-Za-z0-9_.-]*)/init/bash): No such file or directory\r?$"
)
_MESSAGES = {
    "cluster_removed_unresolved": ("warning", "Cluster removed with unresolved work",
        "This cluster configuration was removed while execution, accounting or output collection was unresolved. The status shown below is the last recorded status, not a current scheduler observation.",
        "Check the retained Slurm job identity and accounting independently. Remote jobs may continue, and budget holds remain until final accounting is established."),
    "submission_uncertain": ("warning", "Submission outcome is uncertain",
        "The broker cannot yet determine whether the scheduler accepted this attempt. A local timeout or lost connection is not proof of rejection.",
        "Keep this attempt and its reservation. Reconnect the connector and let reconciliation resolve it; do not submit a replacement as a retry."),
    "rejected": ("error", "Submission was definitively rejected",
        "The broker recorded definitive non-acceptance and released this reservation. No native job identity is recorded.",
        "Review the selected partition, account, QoS and resource limits before preparing a new request. No automatic rerun is performed."),
    "out_of_memory": ("error", "Slurm reported a memory failure",
        "The recorded native state is OUT_OF_MEMORY. This does not identify which process or allocation setting caused it.",
        "Inspect the job logs and memory request. If appropriate, prepare a reviewed revision with a suitable memory limit."),
    "timeout": ("error", "Slurm reported a time limit",
        "The recorded native state is TIMEOUT. The scheduler reports that the allocation reached its time limit.",
        "Check the workload and requested walltime, then prepare a reviewed revision if more time is justified."),
    "node_failure": ("error", "Slurm reported a node or boot failure",
        "The recorded native state identifies a node or boot failure; it does not establish a fault in the Python program.",
        "Check site notices or contact the cluster operator. Review accounting and outputs before deciding whether to prepare a new job."),
    "cancelled": ("warning", "Job cancelled",
        "The broker or scheduler records a cancellation. This summary does not identify who cancelled it or why.",
        "Review the logs and accounting. A cancellation does not by itself mean that no compute time was used."),
    "cancel_pending": ("warning", "Cancellation is awaiting confirmation",
        "A cancellation has been requested, but a confirmed terminal scheduler result is not recorded yet.",
        "Keep the connector available for reconciliation. Do not treat this request as a refund or proof that the allocation has stopped."),
    "cancel_unconfirmed": ("warning", "Cancellation could not be confirmed",
        "The scheduler or transport did not confirm the stop request. The broker retains cancellation intent and keeps the compute reservation.",
        "Keep the connector available for bounded cancellation retries and monitoring. This status does not mean that the allocation has stopped."),
    "cancel_identity_mismatch": ("warning", "Cancellation paused because job identity did not match",
        "The broker refused to cancel an allocation whose live scheduler identity did not match the recorded attempt. Cancellation intent and accounting remain recorded.",
        "Check this attempt with the cluster operator before requesting cancellation again. Monitoring continues; no other job is selected as a substitute."),
    "module_initializer_missing": ("error", "Module initialization file is missing",
        "The job's environment setup reports that its configured module initialization file could not be found. This is not evidence that the Python script ran.",
        "Create a corrected recipe using the cluster-provided module command, then prepare a new job for review. The saved failed package stays unchanged."),
    "module_command_unavailable": ("error", "The module command is unavailable",
        "The environment wrapper reports that no module command is available in this job's environment. This does not establish that Python started.",
        "Check the cluster's login-shell setup or choose an existing Python environment, then prepare and review a new job."),
    "module_initializer_unreadable": ("error", "Module initialization file could not be read",
        "The environment wrapper reports that the configured initialization file is not readable. It may be missing or inaccessible; this does not establish that Python started.",
        "Create a corrected recipe using the cluster-provided module command, then prepare a new job for review. An explicit initializer is an advanced fallback."),
    "outputs_unavailable": ("warning", "Job outputs could not be retained",
        "A reported output-collection or storage failure prevented retention of one or more declared results. This does not prove that the program failed or that the files were never created.",
        "Check the declared output paths, job logs and broker storage status. Keep the existing attempt identity and accounting; do not automatically rerun the compute job."),
    "connector_unavailable": ("warning", "Connector access needs attention",
        "A connector authentication or transport error was reported. Private SSH diagnostics are not included in this summary.",
        "Check the protected connector locally and complete any required SSH/MFA login. A transport failure alone does not prove that a job was not submitted."),
    "preempted": ("warning", "Slurm reported preemption",
        "The saved native state is PREEMPTED. This snapshot alone does not establish whether recovery or final accounting has completed.",
        "Wait for the normal reconciliation result and check site policy before requesting a replacement job."),
    "scheduler_failure": ("error", "Slurm reported an allocation failure",
        "The recorded scheduler state indicates that this allocation did not complete successfully. The specific cause is not established here.",
        "Inspect the saved scheduler facts and job logs. Prepare a new reviewed request only after understanding the result."),
    "failed": ("error", "Job failed",
        "This attempt failed. The saved exit status and logs are evidence to inspect; this summary cannot determine a more specific cause.",
        "Read standard error below and check the selected environment, inputs and resource request before preparing a reviewed revision."),
    "expired": ("warning", "Execution expired",
        "The broker records this attempt as expired. This is distinct from a reported Slurm TIMEOUT state.",
        "Inspect its submission identity, logs and accounting before preparing another request."),
    "succeeded": ("success", "Job completed",
        "The broker records successful completion. Retained outputs and accounting are shown separately.",
        "Inspect the declared results and final accounting; completion alone does not validate the scientific result."),
    "in_progress": ("info", "Execution is still in progress",
        "No terminal failure is recorded for this attempt. Missing logs or an exit status can be normal before collection finishes.",
        "Keep the connector available and reload after the next observation. Do not infer failure from an empty log preview."),
    "unknown": ("warning", "Execution result is not established",
        "The saved facts do not establish a terminal result or a specific failure cause.",
        "Allow the connector to reconcile this attempt and inspect later observations before taking further action."),
}


def _uuid(value):
    try:
        return str(UUID(str(value))) if isinstance(value, (UUID, str)) else None
    except (ValueError, AttributeError):
        return None


def _integer(value):
    return value if type(value) is int and 0 <= value <= _SAFE_INT else None


def _log(value):
    if not isinstance(value, str):
        return "", False
    prefix = value[:MAX_LOG_BYTES]
    encoded = prefix.encode("utf-8", errors="replace")
    truncated = len(value) > len(prefix) or len(encoded) > MAX_LOG_BYTES
    return encoded[:MAX_LOG_BYTES].decode("utf-8", errors="ignore"), truncated


def _category(attempt, state, native_state, error, stderr):
    if state not in {"succeeded", "failed", "cancelled", "expired"}:
        if error == "CANCEL_IDENTITY_MISMATCH":
            return "cancel_identity_mismatch"
        if error == "CANCEL_UNCONFIRMED":
            return "cancel_unconfirmed"
    if state == "submission_uncertain":
        return "submission_uncertain"
    if (state == "failed" and getattr(attempt, "accounting_state", None) == "released"
            and getattr(attempt, "dispatch_started", None) is True and getattr(attempt, "native_identity", None) in (None, {})):
        return "rejected"
    if state in {"cancel_requested", "cancel_pending"}:
        return "cancel_pending"
    if native_state == "OUT_OF_MEMORY":
        return "out_of_memory"
    if native_state == "TIMEOUT":
        return "timeout"
    if native_state in {"NODE_FAIL", "BOOT_FAIL"}:
        return "node_failure"
    if native_state == "CANCELLED" or state == "cancelled":
        return "cancelled"
    if native_state == "PREEMPTED":
        return "preempted"
    if native_state in {"DEADLINE", "REVOKED"}:
        return "scheduler_failure"
    if state == "failed" or native_state == "FAILED":
        if re.search(r"(?m)^BROKER_ENVIRONMENT_ERROR: ?MODULE_INITIALIZER_MISSING(?::[^\r\n]*)?\r?$", stderr) or _MODULE_INITIALIZER.search(stderr):
            return "module_initializer_missing"
        if re.search(r"(?m)^BROKER_ENVIRONMENT_ERROR: ?MODULE_INITIALIZER_UNREADABLE(?::[^\r\n]*)?\r?$", stderr):
            return "module_initializer_unreadable"
        if re.search(r"(?m)^BROKER_ENVIRONMENT_ERROR: ?MODULE_COMMAND_UNAVAILABLE(?::[^\r\n]*)?\r?$", stderr):
            return "module_command_unavailable"
        return "failed"
    if error in {"OUTPUT_COLLECTION_FAILED", "OUTPUT_STORAGE_UNAVAILABLE", "OUTPUT_MISSING", "MISSING_OUTPUT"}:
        return "outputs_unavailable"
    if error in {"AUTHENTICATION_UNAVAILABLE", "SSH_LOGIN_FAILED", "SSH_UNAVAILABLE", "SSH_TIMEOUT", "TRANSPORT_UNAVAILABLE"}:
        return "connector_unavailable"
    if state == "expired":
        return "expired"
    if state == "succeeded":
        return "succeeded"
    if state in {"authorized", "queued", "running"}:
        return "in_progress"
    return "unknown"


def build_attempt_diagnostics(attempt):
    """Return safe display fields; the caller must already authorize the human."""
    observation = getattr(attempt, "observation", None)
    observation = observation if isinstance(observation, dict) else {}
    identity = getattr(attempt, "native_identity", None)
    identity = identity if isinstance(identity, dict) else {}
    accounting = observation.get("accounting")
    accounting = accounting if isinstance(accounting, dict) else {}
    raw_state = getattr(attempt, "state", None)
    state = raw_state if isinstance(raw_state, str) and raw_state in _STATES else "unknown"
    raw_native = observation.get("native_state")
    native_state = None
    if isinstance(raw_native, str) and len(raw_native) <= 100:
        normalized = raw_native.split("+", 1)[0].split(" ", 1)[0]
        native_state = normalized if normalized in _NATIVE_STATES else None
    raw_error = getattr(attempt, "error_code", None)
    error = raw_error if isinstance(raw_error, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", raw_error) else None
    raw_job = identity.get("job_id")
    native_job_id = raw_job if isinstance(raw_job, str) and re.fullmatch(r"[0-9]{1,20}", raw_job) else None
    raw_exit = observation.get("exit_code")
    exit_code = raw_exit if isinstance(raw_exit, str) and re.fullmatch(r"[0-9]{1,10}:[0-9]{1,10}", raw_exit) else None
    stdout, stdout_truncated = _log(getattr(attempt, "stdout", None))
    stderr, stderr_truncated = _log(getattr(attempt, "stderr", None))
    category = _category(attempt, state, native_state, error, stderr)
    connection = getattr(attempt, "connection", None)
    connection_removed = getattr(connection, "archived", False) is True
    if connection_removed and (state not in {"succeeded", "failed", "cancelled", "expired"}
            or getattr(attempt, "accounting_state", None) not in {"settled", "released"}
            or getattr(attempt, "collection_pending", False)):
        category = "cluster_removed_unresolved"
    severity, title, message, suggestion = _MESSAGES[category]
    raw_accounting = getattr(attempt, "accounting_state", None)
    accounting_state = raw_accounting if isinstance(raw_accounting, str) and raw_accounting in _ACCOUNTING else "unknown"
    notices = []
    collection_state = getattr(attempt, "collection_state", None)
    if collection_state not in {"pending", "complete", "retry_pending", "failed"}:
        collection_state = "unknown"
    log_state = getattr(attempt, "log_state", {})
    log_updates = [value["updated_at"] for value in log_state.values()
        if isinstance(value, dict) and isinstance(value.get("updated_at"), str)] if isinstance(log_state, dict) else []
    if collection_state == "retry_pending" and not connection_removed:
        notices.append("Output collection will retry automatically. Compute will not run again.")
    elif collection_state == "retry_pending":
        notices.append("Output collection remains pending. Its connector may no longer be available after removal.")
    if accounting_state == "settled":
        notices.append("Accounting is settled. A failed allocation can still consume compute allowance." if state == "failed" else "Accounting is settled.")
    elif accounting_state in {"reserved", "settlement_pending"}:
        notices.append("Accounting is not settled; reservations can remain held while reconciliation waits for usable evidence.")
    elif accounting_state == "released":
        notices.append("The broker released this reservation through its accounting path.")
    if error in {"OUTPUT_COLLECTION_FAILED", "OUTPUT_STORAGE_UNAVAILABLE", "OUTPUT_MISSING", "MISSING_OUTPUT"} and category != "outputs_unavailable":
        notices.append("Some declared outputs could not be collected or stored; this is separate from the allocation result and accounting.")
    if error == "OBSERVED_OVERAGE":
        notices.append("Actual usage exceeded the reservation and was charged in full. Inspect the remaining project allowance and any debt before requesting more work.")
    raw_name = getattr(connection, "name", None)
    cluster_name = raw_name[:100] if isinstance(raw_name, str) else "Cluster not recorded"
    source = {"sacct": "Slurm accounting", "squeue": "Slurm queue", "none": "No scheduler observation"}.get(observation.get("source")) if isinstance(observation.get("source"), str) else None
    return {"attempt_id": _uuid(getattr(attempt, "id", None)), "job_id": _uuid(getattr(attempt, "job_id", None)),
        "connection_id": _uuid(getattr(attempt, "connection_id", None)), "cluster_name": cluster_name,
        "connection_removed": connection_removed,
        "state": state, "native_job_id": native_job_id, "native_state": native_state, "exit_code": exit_code,
        "error_code": error, "accounting_state": accounting_state, "source": source,
        "elapsed_seconds": _integer(observation.get("elapsed_seconds")),
        "allocated_cpus": _integer(observation.get("allocated_cpus")), "allocated_gpus": _integer(observation.get("allocated_gpus")),
        "cpu_core_seconds": _integer(accounting.get("cpu_core_seconds")), "gpu_device_seconds": _integer(accounting.get("gpu_device_seconds")),
        "accounting_final": accounting.get("final") if type(accounting.get("final")) is bool else None,
        "category": category, "severity": severity, "title": title, "message": message, "suggestion": suggestion,
        "notices": notices, "stdout": stdout, "stderr": stderr, "stdout_truncated": stdout_truncated,
        "collection_state": collection_state, "log_updated_at": max(log_updates) if log_updates else None,
        "stderr_truncated": stderr_truncated, "log_limit_bytes": MAX_LOG_BYTES}
