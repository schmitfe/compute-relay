"""Durable project/key recovery inbox; listing never admits or cancels work."""

import base64
import json
from uuid import UUID

from django.db.models import Exists, OuterRef, Q
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .auth import connection_allowed
from .models import Attempt, ExecutionRequest, Job, ResultAccess
from .errors import BrokerError
from .views import boundary, body, scope

TERMINAL = {"succeeded", "failed", "cancelled", "expired"}


def record_access(credential, attempt, field):
    row, _ = ResultAccess.objects.get_or_create(credential=credential, attempt=attempt)
    setattr(row, field, timezone.now())
    row.save(update_fields=[field])


def recovery_jobs(credential):
    connections = [str(item.id) for item in credential.project.connections.all() if connection_allowed(credential, item.id)]
    attempts = Attempt.objects.filter(job_id=OuterRef("pk"))
    unread = attempts.filter(state__in=TERMINAL).annotate(seen=Exists(ResultAccess.objects.filter(
        attempt_id=OuterRef("pk"), credential=credential).filter(Q(bytes_requested_at__isnull=False) | Q(acknowledged_at__isnull=False))))
    unacknowledged = attempts.filter(state__in=TERMINAL).annotate(ack=Exists(ResultAccess.objects.filter(
        attempt_id=OuterRef("pk"), credential=credential, acknowledged_at__isnull=False)))
    return Job.objects.with_summary().filter(project=credential.project, request__connection_id__in=connections).exclude(state="archived").annotate(
        active=Exists(attempts.filter(~Q(state__in=TERMINAL) | ~Q(accounting_state__in=["settled", "released"]) | Q(collection_pending=True))),
        review_pending=Exists(ExecutionRequest.objects.filter(job_id=OuterRef("pk"), state="awaiting_approval")),
        never_executed=~Exists(ExecutionRequest.objects.filter(job_id=OuterRef("pk"))),
        superseded=Exists(Job.objects.filter(previous_id=OuterRef("pk")).exclude(state="archived")),
        unread=Exists(unread.filter(seen=False)), unacknowledged=Exists(unacknowledged.filter(ack=False)))


def select_jobs(query, state):
    unfinished = Q(active=True) | Q(review_pending=True) | Q(never_executed=True, superseded=False)
    return query.filter({"all": Q(), "unfinished": unfinished & ~Q(state="cancelled"), "unread_results": Q(unread=True),
        "attention": (unfinished | Q(unacknowledged=True)) & ~Q(state="cancelled")}[state])


def summary(credential):
    query = recovery_jobs(credential)
    return {"unfinished": select_jobs(query, "unfinished").count(), "unread_results": query.filter(unread=True).count(),
        "results_awaiting_acknowledgement": query.filter(unacknowledged=True).count(),
        "recovery_tool": "list_project_jobs", "resubmission_authorized": False}


@boundary("GET")
def jobs(request, project_id):
    credential, _ = scope(request, "jobs:read", project_id=project_id)
    state = request.GET.get("state", "attention")
    limit = int(request.GET.get("limit", 20))
    if state not in {"attention", "unfinished", "unread_results", "all"} or not 1 <= limit <= 100:
        raise ValueError("Invalid recovery filter")
    query = select_jobs(recovery_jobs(credential), state)
    if "context" in request.GET:
        if len(request.GET["context"]) > 512:
            raise ValueError("Invalid context filter")
        from compute_relay.extensions import filter_job_context
        query = filter_job_context(query, credential, json.loads(request.GET["context"]))
    cursor = request.GET.get("cursor")
    if cursor:
        if len(cursor) > 512:
            raise ValueError("Invalid cursor")
        instant, identifier = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        instant = parse_datetime(instant)
        identifier = UUID(identifier)
        if instant is None or timezone.is_naive(instant):
            raise ValueError("Invalid cursor timestamp")
        query = query.filter(Q(created_at__lt=instant) | Q(created_at=instant, id__lt=identifier))
    rows = list(query.order_by("-created_at", "-id")[:limit + 1])
    result = []
    for job in rows[:limit]:
        attempts = list(job.attempts.order_by("-created_at", "-id")[:21])
        access = {row.attempt_id: row for row in ResultAccess.objects.filter(credential=credential, attempt__job=job)}
        result.append({"id": str(job.id), "name": job.name, "state": job.display_state, "created_at": job.created_at.isoformat(),
            "connection_id": job.request["connection_id"], "prepared": job.manifest is not None,
            "attention": {"active": job.active, "awaiting_review": job.review_pending,
                "prepared_without_execution": job.never_executed and not job.superseded, "unread_results": job.unread},
            "attempts": [{"id": str(attempt.id), "state": attempt.state, "accounting_state": attempt.accounting_state,
                "collection_state": attempt.collection_state, "inventory_seen": bool(access.get(attempt.id) and access[attempt.id].inventory_seen_at),
                "result_bytes_requested": bool(access.get(attempt.id) and access[attempt.id].bytes_requested_at),
                "acknowledged": bool(access.get(attempt.id) and access[attempt.id].acknowledged_at)} for attempt in attempts[:20]],
            "attempts_truncated": len(attempts) > 20,
            "next_tool": "get_job"})
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([last.created_at.isoformat(), str(last.id)]).encode()).decode()
    return {"project_id": str(credential.project_id), "state": state, "jobs": result, "next_cursor": next_cursor,
        "result_tracking": "observations_since_upgrade; byte requests do not establish complete downloads or scientific validation"}


@boundary("POST")
def acknowledge(request, attempt_id):
    credential, attempt = scope(request, "jobs:read", attempt_id=attempt_id)
    if body(request) or attempt.state not in TERMINAL:
        raise ValueError("Only finished attempts can be acknowledged")
    record_access(credential, attempt, "acknowledged_at")
    return {"attempt_id": str(attempt.id), "acknowledged": True, "scientific_validation": "not_asserted"}


@boundary("POST")
def cancel_prepared(request, job_id):
    credential, job = scope(request, "jobs:cancel", job_id=job_id)
    if body(request):
        raise ValueError("Cancellation accepts an empty object")
    with transaction.atomic():
        from .models import Project
        Project.objects.select_for_update().get(id=job.project_id)
        job = Job.objects.select_for_update().get(id=job.id)
        if job.execution_requests.exists() or job.state not in {"draft", "prepared", "cancelled"}:
            raise BrokerError("JOB_HAS_EXECUTIONS", "Use execution-request or attempt cancellation for this job.", 409)
        job.state = "cancelled"
        job.save(update_fields=["state"])
    return {"job_id": str(job.id), "state": "cancelled", "compute_submitted": False}
