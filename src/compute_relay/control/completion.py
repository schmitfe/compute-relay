"""Read-only completion snapshots. Local verification and science stay explicit."""

from collections import defaultdict
import hashlib
import json
import re
import threading
import time

from django.db.models import Count, Exists, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from .database import read_snapshot

from .errors import BrokerError
from .models import Artifact, LedgerEntry, ResultAccess, WorkerReport
from .services import serialize_attempt

TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
FINAL = {"settled", "released"}
_wait_slots = threading.BoundedSemaphore(2)


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()).hexdigest()


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


def job_snapshot(credential, job, *, attempt_cursor=0, execution_cursor=0, limit=20):
    if (any(type(cursor) is not int or not 0 <= cursor <= 2**53 - 1 for cursor in (attempt_cursor, execution_cursor))
            or type(limit) is not int or not 1 <= limit <= 20):
        raise BrokerError("INVALID_REQUEST", "Use bounded nonnegative cursors and a limit of 1 through 20.")
    from .input_sources import authorize_job_inputs
    authorize_job_inputs(job, credential)
    attempts = job.attempts.all()
    executions = job.execution_requests.all()
    access = ResultAccess.objects.filter(credential=credential, attempt_id=OuterRef("pk"))
    pending_accounting = ~Q(accounting_state__in=FINAL)
    no_identity = Q(native_identity__isnull=True) | Q(native_identity={})
    stats = attempts.annotate(acknowledged=Exists(access.filter(acknowledged_at__isnull=False))).aggregate(
        total=Count("id"), confirmed=Count("id", filter=~no_identity),
        dispatched=Count("id", filter=Q(dispatch_started=True)),
        uncertain=Count("id", filter=Q(dispatch_started=True) & no_identity & pending_accounting),
        accounting_pending=Count("id", filter=pending_accounting),
        unacknowledged=Count("id", filter=Q(state__in=TERMINAL, acknowledged=False)),
        cpu=Sum("reservation_cpu", filter=pending_accounting), gpu=Sum("reservation_gpu", filter=pending_accounting))
    execution_stats = executions.aggregate(total=Count("id"), pending=Count("id", filter=Q(state="awaiting_approval")),
        cpu=Sum("reservation_cpu", filter=Q(reservation_state="held")),
        gpu=Sum("reservation_gpu", filter=Q(reservation_state="held")))
    totals = LedgerEntry.objects.filter(attempt__job=job, action="settle").aggregate(cpu=Sum("cpu"), gpu=Sum("gpu"))
    declared = job.request.get("outputs", [])
    can_read = "artifacts:read" in credential.scopes
    blockers, blocker_count = [], 0
    def block(code, key, identifier):
        nonlocal blocker_count
        blocker_count += 1
        if len(blockers) < 20:
            blockers.append({"code": code, key: str(identifier)})

    # Full-job revision inputs stay compact: reports already hash their logs and
    # observations at publication. Never load those retained bytes for polling.
    token = hashlib.sha256()
    def remember(value):
        token.update(revision(value).encode())
    output_counts = Artifact.objects.filter(attempt_id=OuterRef("pk"), path__in=declared).values("attempt_id").annotate(
        total=Count("id")).values("total")
    receipt = WorkerReport.objects.filter(attempt_id=OuterRef("pk")).order_by("-generation", "lease_id")
    compact = attempts.annotate(output_count=Coalesce(Subquery(output_counts), Value(0), output_field=IntegerField()) if can_read else Value(0),
        receipt=Subquery(receipt.values("payload_sha256")[:1])).order_by("created_at", "id").values(
        "id", "execution_request_id", "state", "accounting_state", "native_identity", "error_code",
        "collection_pending", "collection_state", "dispatch_started", "reservation_cpu", "reservation_gpu",
        "output_count", "receipt")
    for attempt in compact.iterator(chunk_size=200):
        remember(attempt)
        if attempt["state"] not in TERMINAL:
            block("EXECUTION_UNFINISHED", "attempt_id", attempt["id"])
        if attempt["accounting_state"] not in FINAL:
            block("ACCOUNTING_PENDING", "attempt_id", attempt["id"])
        if attempt["collection_pending"]:
            block("COLLECTION_PENDING", "attempt_id", attempt["id"])
        elif attempt["collection_state"] == "cleaned":
            block("OUTPUTS_CLEANED", "attempt_id", attempt["id"])
        elif attempt["collection_state"] == "failed":
            block("COLLECTION_FAILED", "attempt_id", attempt["id"])
        elif attempt["state"] == "succeeded" and declared and (not can_read or attempt["output_count"] < len(set(declared))):
            block("OUTPUTS_MISSING" if can_read else "OUTPUTS_UNVERIFIED", "attempt_id", attempt["id"])
    for item in executions.order_by("created_at", "id").values("id", "state", "reservation_state",
            "reservation_cpu", "reservation_gpu", "approval_requirements", "credential__execution_mode",
            "approvaldecision__comment").iterator(chunk_size=200):
        remember(item)
        if item["state"] == "awaiting_approval":
            block("APPROVAL_PENDING", "execution_request_id", item["id"])
    if not execution_stats["total"] and job.state not in {"cancelled", "archived"}:
        block("UNEXECUTED_PACKAGE", "job_id", job.id)
    for row in ResultAccess.objects.filter(credential=credential, attempt__job=job).order_by("attempt_id").values(
            "attempt_id", "inventory_seen_at", "bytes_requested_at", "acknowledged_at").iterator(chunk_size=200):
        remember(row)
    if can_read:
        for row in Artifact.objects.filter(attempt__job=job).order_by("path", "id").values(
                "id", "attempt_id", "path", "blob__sha256", "blob__size_bytes").iterator(chunk_size=200):
            remember(row)
    summary = {"operationally_reconciled": not blocker_count, "blockers": blockers,
        "blocker_count": blocker_count, "blockers_truncated": blocker_count > len(blockers),
        "execution_requests": execution_stats["total"], "pending_approvals": execution_stats["pending"], "attempts": stats["total"],
        "confirmed_submissions": stats["confirmed"], "dispatch_started": stats["dispatched"], "uncertain_submissions": stats["uncertain"],
        "spent": {"cpu_core_seconds": totals["cpu"] or 0, "gpu_device_seconds": totals["gpu"] or 0},
        "reserved": {"cpu_core_seconds": (stats["cpu"] or 0) + (execution_stats["cpu"] or 0),
            "gpu_device_seconds": (stats["gpu"] or 0) + (execution_stats["gpu"] or 0)},
        "accounting_final": not stats["accounting_pending"] and not execution_stats["pending"],
        "unacknowledged_attempts": stats["unacknowledged"],
        "local_hash_verification": "not_observed_by_broker", "scientific_validation": "not_assessed_by_broker",
        "client_permission_review": "not_observed_by_broker", "monetary_cost": "not_configured"}
    package = {"manifest_sha256": job.manifest_sha256, "connection_id": job.request.get("connection_id"),
        "command": job.request.get("command"), "environment": job.request.get("environment"), "resources": job.request.get("resources"),
        "declared_outputs": declared, "inputs": [{"path": entry.path, "sha256": entry.blob.sha256,
            "size_bytes": entry.blob.size_bytes, "executable": entry.executable,
            "source_artifact_id": str(entry.source_artifact_id) if entry.source_artifact_id else None}
            for entry in job.files.select_related("blob").order_by("path")]}
    page = list(attempts.select_related("connection").order_by("created_at", "id")[attempt_cursor:attempt_cursor + limit])
    page_access = {row.attempt_id: row for row in ResultAccess.objects.filter(credential=credential, attempt__in=page)}
    inventory = defaultdict(list)
    if can_read:
        for artifact in Artifact.objects.filter(attempt__in=page).select_related("blob").order_by("path", "id"):
            inventory[artifact.attempt_id].append({"id": str(artifact.id), "path": artifact.path,
                "sha256": artifact.blob.sha256, "size_bytes": artifact.blob.size_bytes})
    records = []
    for attempt in page:
        item = serialize_attempt(attempt)
        item["dispatch_started"] = attempt.dispatch_started
        item["submission_uncertain"] = attempt.dispatch_started and not attempt.native_identity and attempt.accounting_state not in FINAL
        item["compute_submitted"] = True if attempt.native_identity else None if item["submission_uncertain"] else False
        row = page_access.get(attempt.id)
        item["delivery"] = {"inventory_seen": bool(row and row.inventory_seen_at),
            "bytes_requested": bool(row and row.bytes_requested_at), "acknowledged": bool(row and row.acknowledged_at),
            "local_hash_verification": "not_observed_by_broker"}
        item["artifacts"] = inventory[attempt.id] if can_read else None
        item["missing_outputs"] = sorted(set(declared) - {entry["path"] for entry in inventory[attempt.id]}) if can_read else None
        records.append(item)
    payload = {"schema_version": "1.0", "job": {"id": str(job.id), "name": job.name, "package_state": job.state,
        "previous_job_id": str(job.previous_id) if job.previous_id else None, "created_at": job.created_at.isoformat()},
        "package": package, "summary": summary,
        "execution_requests": [{"id": str(item.id), "state": item.state,
            "budget_reservation_state": item.reservation_state} for item in executions.order_by("created_at", "id")[execution_cursor:execution_cursor + limit]],
        "attempts": records, "next_cursor": attempt_cursor + limit if stats["total"] > attempt_cursor + limit else None,
        "next_execution_cursor": execution_cursor + limit if execution_stats["total"] > execution_cursor + limit else None}
    remember({key: payload[key] for key in ("schema_version", "job", "package", "summary")})
    payload["snapshot_revision"] = token.hexdigest()
    return payload


def completion_view(request, job_id):
    from .views import scope
    with read_snapshot():
        credential, job = scope(request, "jobs:read", job_id=job_id)
        result = job_snapshot(credential, job, attempt_cursor=int(request.GET.get("cursor", 0)),
            execution_cursor=int(request.GET.get("execution_cursor", 0)), limit=int(request.GET.get("limit", 20)))
    from django.http import JsonResponse
    response = JsonResponse(result)
    response["Cache-Control"] = "no-store, private"
    return response
