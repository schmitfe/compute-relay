"""Durable burst rates and unfinished-work bounds, independent of spending."""

import math

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from .errors import BrokerError
from .models import Attempt, ExecutionRequest, Job, MetadataPool, Project


OPERATIONS = ("create", "execute")


def outstanding(project):
    """A first execution replaces its draft slot; each further execution adds one."""
    drafts = Job.objects.filter(state__in=["draft", "prepared"]).filter(
        ~Exists(ExecutionRequest.objects.filter(job_id=OuterRef("id"))))
    reviews = ExecutionRequest.objects.filter(state="awaiting_approval", attempt__isnull=True)
    attempts = Attempt.objects.exclude(state__in=["succeeded", "failed", "cancelled", "expired"],
        accounting_state__in=["settled", "released"], collection_pending=False)
    counts = {"project": 0, "installation": 0}
    for rows, field in [(drafts, "project"), (reviews, "job__project"), (attempts, "job__project")]:
        usage = rows.aggregate(project=Count("id", filter=Q(**{field: project})), installation=Count("id"))
        for scope in counts:
            counts[scope] += usage[scope]
    return {scope: {"used": used, "limit": getattr(settings, "MAX_" + scope.upper() + "_OUTSTANDING_JOBS"),
                   "remaining": max(0, getattr(settings, "MAX_" + scope.upper() + "_OUTSTANDING_JOBS") - used)}
            for scope, used in counts.items()}


def _bucket(row, scope, operation, now):
    burst = getattr(settings, scope.upper() + "_ADMISSION_BURST")
    per_minute = getattr(settings, scope.upper() + "_ADMISSION_PER_MINUTE")
    previous = row.admission_state.get(operation)
    tokens = burst if previous is None else min(burst,
        previous["tokens"] + max(0, now - previous["at"]) * per_minute / 60)
    delay = max(0, previous["at"] - now) if previous else 0
    return tokens, {"limit": burst, "per_minute": per_minute, "remaining": math.floor(tokens),
                    "retry_after_seconds": max(1, math.ceil(delay + (1 - tokens) * 60 / per_minute)) if tokens < 1 else 0}


def admission_snapshot(project):
    current = Project.objects.get(id=project.id)
    pool = MetadataPool.objects.filter(id=1).first() or MetadataPool()
    now = timezone.now().timestamp()
    return {"outstanding": outstanding(project), "rates": {
        operation: {scope: _bucket(row, scope, operation, now)[1]
                    for scope, row in [("project", current), ("installation", pool)]}
        for operation in OPERATIONS}}


@transaction.atomic
def admit(project, operation, *, additional=1):
    """Caller includes this charge, its work and its idempotency receipt in one commit."""
    current = Project.objects.select_for_update().get(id=project.id)
    MetadataPool.objects.get_or_create(id=1)
    pool = MetadataPool.objects.select_for_update().get(id=1)
    for scope, capacity in outstanding(project).items():
        if capacity["used"] + additional > capacity["limit"]:
            raise BrokerError("BACKLOG_LIMIT", f"The {scope} has {capacity['used']} outstanding jobs "
                f"(limit {capacity['limit']}). Wait for work to finish or archive unused drafts.", 409,
                {"scope": scope, **capacity, "requested": additional, "retry_after_seconds": None})
    now = timezone.now().timestamp()
    buckets = [(scope, row, *_bucket(row, scope, operation, now))
               for scope, row in [("project", current), ("installation", pool)]]
    for scope, row, tokens, capacity in buckets:
        if tokens < 1:
            raise BrokerError("RATE_LIMITED", f"The {scope} {operation} burst is exhausted. "
                f"Retry in {capacity['retry_after_seconds']} seconds.", 429,
                {"scope": scope, "operation": operation, **capacity})
    for scope, row, tokens, capacity in buckets:
        previous = row.admission_state.get(operation, {})
        row.admission_state[operation] = {"tokens": tokens - 1, "at": max(now, previous.get("at", now))}
        row.save(update_fields=["admission_state"])
