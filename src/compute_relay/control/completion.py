"""Read-only completion snapshots. Local verification and science stay explicit."""

from collections import defaultdict
import hashlib
import json
import re
import threading
import time

from django.db.models import Sum
from django.db import transaction

from .errors import BrokerError
from .models import Artifact, LedgerEntry, ResultAccess
from .services import serialize_attempt

TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
FINAL = {"settled", "released"}
_wait_slots = threading.BoundedSemaphore(2)


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def wait_for_snapshot(read, after_revision=None, wait_seconds=0):
    if (type(wait_seconds) is not int or not 0 <= wait_seconds <= 20
            or after_revision is not None and (not isinstance(after_revision, str)
                or not re.fullmatch(r"[0-9a-f]{64}", after_revision))
            or wait_seconds and after_revision is None):
        raise BrokerError("INVALID_WAIT", "Use a snapshot revision and an integer wait of 0 through 20 seconds.")
    acquired = False
    try:
        if wait_seconds:
            acquired = _wait_slots.acquire(blocking=False)
            if not acquired:
                raise BrokerError("WAIT_BUSY", "Status wait capacity is busy; retry shortly.", 429)
        deadline = time.monotonic() + wait_seconds
        while True:
            # Each read rechecks current authority and ends its own database
            # transaction. Sleeping never holds a database or project lock.
            value = read()
            token = revision(value)
            changed = token != after_revision
            remaining = deadline - time.monotonic()
            if changed or remaining <= 0:
                return {**value, "revision": token, "changed": changed}
            time.sleep(min(.5, remaining))
    finally:
        if acquired:
            _wait_slots.release()


def job_snapshot(credential, job, *, attempt_cursor=0, limit=20):
    if type(attempt_cursor) is not int or attempt_cursor < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise BrokerError("INVALID_REQUEST", "Use a nonnegative attempt cursor and a limit of 1 through 20.")
    from .input_sources import authorize_job_inputs
    authorize_job_inputs(job, credential)
    attempts = list(job.attempts.select_related("job", "connection").order_by("created_at", "id"))
    executions = list(job.execution_requests.order_by("created_at", "id"))
    access = {row.attempt_id: row for row in ResultAccess.objects.filter(credential=credential, attempt__job=job)}
    inventory = defaultdict(list)
    can_read = "artifacts:read" in credential.scopes
    if can_read:
        for artifact in Artifact.objects.filter(attempt__job=job).select_related("blob").order_by("path", "id"):
            inventory[artifact.attempt_id].append({"id": str(artifact.id), "path": artifact.path,
                "sha256": artifact.blob.sha256, "size_bytes": artifact.blob.size_bytes})
    declared = job.request.get("outputs", [])
    records, blockers = [], []
    for attempt in attempts:
        item = serialize_attempt(attempt)
        item["dispatch_started"] = attempt.dispatch_started
        item["submission_uncertain"] = attempt.dispatch_started and not attempt.native_identity and attempt.accounting_state not in FINAL
        item["compute_submitted"] = True if attempt.native_identity else None if item["submission_uncertain"] else False
        row = access.get(attempt.id)
        item["delivery"] = {"inventory_seen": bool(row and row.inventory_seen_at),
            "bytes_requested": bool(row and row.bytes_requested_at),
            "acknowledged": bool(row and row.acknowledged_at), "local_hash_verification": "not_observed_by_broker"}
        item["artifacts"] = inventory[attempt.id] if can_read else None
        missing = sorted(set(declared) - {entry["path"] for entry in inventory[attempt.id]}) if can_read else None
        item["missing_outputs"] = missing
        if attempt.state not in TERMINAL:
            blockers.append({"code": "EXECUTION_UNFINISHED", "attempt_id": str(attempt.id)})
        if attempt.accounting_state not in FINAL:
            blockers.append({"code": "ACCOUNTING_PENDING", "attempt_id": str(attempt.id)})
        if attempt.collection_pending:
            blockers.append({"code": "COLLECTION_PENDING", "attempt_id": str(attempt.id)})
        elif attempt.collection_state == "cleaned":
            blockers.append({"code": "OUTPUTS_CLEANED", "attempt_id": str(attempt.id)})
        elif attempt.collection_state == "failed":
            blockers.append({"code": "COLLECTION_FAILED", "attempt_id": str(attempt.id)})
        elif attempt.state == "succeeded" and declared and (missing is None or missing):
            blockers.append({"code": "OUTPUTS_UNVERIFIED" if missing is None else "OUTPUTS_MISSING", "attempt_id": str(attempt.id)})
        records.append(item)
    pending = [item for item in executions if item.state == "awaiting_approval"]
    blockers.extend({"code": "APPROVAL_PENDING", "execution_request_id": str(item.id)} for item in pending)
    if not executions and job.state not in {"cancelled", "archived"}:
        blockers.append({"code": "UNEXECUTED_PACKAGE", "job_id": str(job.id)})
    totals = LedgerEntry.objects.filter(attempt__job=job, action="settle").aggregate(cpu=Sum("cpu"), gpu=Sum("gpu"))
    reserved = {"cpu_core_seconds": sum(item.reservation_cpu for item in attempts if item.accounting_state not in FINAL)
        + sum(item.reservation_cpu for item in executions if item.reservation_state == "held"),
        "gpu_device_seconds": sum(item.reservation_gpu for item in attempts if item.accounting_state not in FINAL)
        + sum(item.reservation_gpu for item in executions if item.reservation_state == "held")}
    summary = {"operationally_reconciled": not blockers, "blockers": blockers,
        "execution_requests": len(executions), "pending_approvals": len(pending), "attempts": len(attempts),
        "confirmed_submissions": sum(bool(item.native_identity) for item in attempts),
        "dispatch_started": sum(item.dispatch_started for item in attempts),
        "uncertain_submissions": sum(item["submission_uncertain"] for item in records),
        "spent": {"cpu_core_seconds": totals["cpu"] or 0, "gpu_device_seconds": totals["gpu"] or 0},
        "reserved": reserved, "accounting_final": all(item.accounting_state in FINAL for item in attempts) and not pending,
        "unacknowledged_attempts": sum(not item["delivery"]["acknowledged"] for item in records if item["state"] in TERMINAL),
        "local_hash_verification": "not_observed_by_broker", "scientific_validation": "not_assessed_by_broker",
        "client_permission_review": "not_observed_by_broker", "monetary_cost": "not_configured"}
    package = {"manifest_sha256": job.manifest_sha256, "connection_id": job.request.get("connection_id"),
        "command": job.request.get("command"), "environment": job.request.get("environment"), "resources": job.request.get("resources"),
        "declared_outputs": declared, "inputs": [{"path": entry.path, "sha256": entry.blob.sha256,
            "size_bytes": entry.blob.size_bytes, "executable": entry.executable,
            "source_artifact_id": str(entry.source_artifact_id) if entry.source_artifact_id else None}
            for entry in job.files.select_related("blob").order_by("path")]}
    payload = {"schema_version": "1.0", "job": {"id": str(job.id), "name": job.name, "package_state": job.state,
        "previous_job_id": str(job.previous_id) if job.previous_id else None, "created_at": job.created_at.isoformat()},
        "package": package, "summary": summary,
        "execution_requests": [{"id": str(item.id), "state": item.state,
            "budget_reservation_state": item.reservation_state} for item in executions],
        "attempts": records[attempt_cursor:attempt_cursor + limit],
        "next_cursor": attempt_cursor + limit if len(records) > attempt_cursor + limit else None}
    # Revision covers the entire job, including attempts outside this page.
    payload["snapshot_revision"] = revision({**payload, "attempts": records, "next_cursor": None})
    return payload


def completion_view(request, job_id):
    from .views import scope
    with transaction.atomic():
        credential, job = scope(request, "jobs:read", job_id=job_id)
        result = job_snapshot(credential, job, attempt_cursor=int(request.GET.get("cursor", 0)), limit=int(request.GET.get("limit", 20)))
    from django.http import JsonResponse
    response = JsonResponse(result)
    response["Cache-Control"] = "no-store, private"
    return response
