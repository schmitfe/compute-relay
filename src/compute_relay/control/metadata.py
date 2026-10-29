"""Transactional logical metadata limits, separate from compute settlement.

These conservative JSON/row charges bound admissions rather than promise an
exact SQLite file size. Durable idempotency identities are never deleted.
"""

import hashlib
import json

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, Sum

from .errors import BrokerError
from .models import AuditEvent, IdempotencyRecord, MetadataPool, MetadataUsage, Project


RECORD_LIMITS = {
    "idempotency": ("MAX_PROJECT_IDEMPOTENCY_KEYS", 10000, "MAX_INSTALLATION_IDEMPOTENCY_KEYS", 100000),
    "definition": ("MAX_PROJECT_DEFINITIONS", 1000, "MAX_INSTALLATION_DEFINITIONS", 10000),
    "retired_job": ("MAX_PROJECT_RETIRED_JOBS", 10000, "MAX_INSTALLATION_RETIRED_JOBS", 100000),
}


def json_size(value):
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


def _limit(name, default):
    return getattr(settings, name, default)


def _lock(project):
    Project.objects.select_for_update().get(id=project.id)
    MetadataPool.objects.get_or_create(id=1)
    return MetadataPool.objects.select_for_update().get(id=1)


def reserve_metadata(project, kind, object_id, size_bytes):
    """Create or resize one logical charge; caller's transaction owns its row."""
    if type(size_bytes) is not int or size_bytes < 0:
        raise ValueError("Invalid metadata charge")
    with transaction.atomic():
        _lock(project)
        current = MetadataUsage.objects.filter(project=project, kind=kind, object_id=str(object_id)).first()
        prior = current.size_bytes if current else 0
        project_rows = MetadataUsage.objects.filter(project=project)
        project_size = project_rows.aggregate(total=Sum("size_bytes"))["total"] or 0
        total = MetadataUsage.objects.aggregate(total=Sum("size_bytes"))["total"] or 0
        # Retained jobs use byte capacity; unfinished work has a separate admission bound.
        if kind != "job" and current is None:
            names = RECORD_LIMITS.get(kind, RECORD_LIMITS["definition"])
            group_rows = (MetadataUsage.objects.filter(kind=kind) if kind in {"idempotency", "retired_job"}
                else MetadataUsage.objects.exclude(kind__in=["job", "idempotency", "retired_job"]))
            if kind == "idempotency":
                # Compaction frees response slots while durable replay identities keep their byte charge.
                group_rows = IdempotencyRecord.objects.filter(compacted=False)
            for scope, rows, name, default in [("project", group_rows.filter(project=project), *names[:2]),
                                                ("installation", group_rows, *names[2:])]:
                used, limit = rows.count(), _limit(name, default)
                if used >= limit:
                    raise BrokerError("METADATA_QUOTA", f"The {scope} {kind} record limit is exhausted "
                        f"({used} of {limit}). Compact unused records before adding more.", 409,
                        {"scope": scope, "resource": kind + "_records", "used": used, "limit": limit, "remaining": 0})
        for scope, used, name, default in [("project", project_size, "PROJECT_METADATA_LIMIT", 32 * 1024**2),
                                            ("installation", total, "GLOBAL_METADATA_LIMIT", 256 * 1024**2)]:
            limit = _limit(name, default)
            if used - prior + size_bytes > limit:
                raise BrokerError("METADATA_QUOTA", f"The {scope} logical metadata limit is exhausted "
                    f"({used} of {limit} bytes). Compact unused records before adding more.", 409,
                    {"scope": scope, "resource": "metadata_bytes", "used": used, "limit": limit,
                     "remaining": max(0, limit - used), "requested": size_bytes - prior})
        MetadataUsage.objects.update_or_create(project=project, kind=kind, object_id=str(object_id),
                                              defaults={"size_bytes": size_bytes})


def retire_metadata(project, object_id, size_bytes):
    """Atomically shrink a job charge into a small, count-capped tombstone.

Unlike admission, a strictly shrinking transfer is permitted while an
installation is over its byte ceiling. It cannot create or enlarge a charge.
Eligibility to archive the corresponding job is enforced by maintenance.
"""
    if type(size_bytes) is not int or not 1 <= size_bytes <= 4096:
        raise ValueError("A retired job charge must contain 1 through 4096 bytes")
    with transaction.atomic():
        _lock(project)
        current = MetadataUsage.objects.select_for_update().filter(
            project=project, kind="job", object_id=str(object_id)).first()
        if current is None or size_bytes >= current.size_bytes:
            raise BrokerError("METADATA_NOT_SHRINKING", "Archival requires an existing, strictly larger job charge.", 409)
        retired = MetadataUsage.objects.filter(kind="retired_job")
        if (retired.filter(project=project).count() >= _limit("MAX_PROJECT_RETIRED_JOBS", 10000)
                or retired.count() >= _limit("MAX_INSTALLATION_RETIRED_JOBS", 100000)):
            raise BrokerError("METADATA_QUOTA", "Retired job identity capacity is exhausted.", 409)
        if retired.filter(project=project, object_id=str(object_id)).exists():
            raise BrokerError("METADATA_NOT_SHRINKING", "This job already has a retained identity.", 409)
        saved = current.size_bytes - size_bytes
        current.kind, current.size_bytes = "retired_job", size_bytes
        current.save(update_fields=["kind", "size_bytes"])
        return saved


def metadata_usage(project=None):
    rows = MetadataUsage.objects.all()
    if project is not None:
        rows = rows.filter(project=project)
    return {"logical_bytes": rows.aggregate(total=Sum("size_bytes"))["total"] or 0,
            "records": rows.count(), "jobs": rows.filter(kind="job").count(),
            "idempotency_keys": rows.filter(kind="idempotency").count(),
            "definitions": rows.exclude(kind__in=["job", "idempotency", "retired_job"]).count()}


def capacity_rows(project):
    """Read-only logical capacity, including pending storage claims; no disk scan."""
    from .models import StoragePool, UploadReservation
    from .storage import MAX_PENDING_UPLOADS
    current = Project.objects.get(id=project.id)
    pool = StoragePool.objects.filter(id=1).first()
    kinds = {"retired_job": Q(kind="retired_job"),
             "definition": ~Q(kind__in=["job", "idempotency", "retired_job"])}
    metadata = MetadataUsage.objects.aggregate(
        global_bytes=Sum("size_bytes"), project_bytes=Sum("size_bytes", filter=Q(project=project)),
        **{scope + kind: Count("id", filter=condition & restriction)
           for kind, condition in kinds.items()
           for scope, restriction in [("project_", Q(project=project)), ("global_", Q())]})
    keys = IdempotencyRecord.objects.filter(compacted=False).aggregate(
        project_idempotency=Count("id", filter=Q(project=project)), global_idempotency=Count("id"))
    metadata.update(keys)
    pending = UploadReservation.objects.filter(project=project, state="reserved").count()
    rows = []

    def row(label, used, limit, global_used=None, global_limit=None, *, bytes=False):
        rows.append({"label": label, "bytes": bytes, "used": used, "limit": limit,
            "remaining": max(0, limit - used), "global_used": global_used, "global_limit": global_limit,
            "global_remaining": max(0, global_limit - global_used) if global_limit is not None else None,
            "full": used >= limit or (global_limit is not None and global_used >= global_limit)})

    row("Stored and reserved file bytes", current.storage_used + current.storage_reserved, settings.PROJECT_STORAGE_LIMIT,
        (pool.used + pool.reserved) if pool else 0, settings.GLOBAL_STORAGE_LIMIT, bytes=True)
    row("Stored files and pending transfers", current.object_count + pending, settings.MAX_OBJECTS)
    row("Incomplete transfers", pending, MAX_PENDING_UPLOADS)
    row("Logical metadata", metadata["project_bytes"] or 0, _limit("PROJECT_METADATA_LIMIT", 32 * 1024**2),
        metadata["global_bytes"] or 0, _limit("GLOBAL_METADATA_LIMIT", 256 * 1024**2), bytes=True)
    for kind, label in [("idempotency", "Active retry receipts"),
                        ("definition", "Module definitions and runs"), ("retired_job", "Archived job identities")]:
        names = RECORD_LIMITS[kind]
        row(label, metadata["project_" + kind], _limit(names[0], names[1]),
            metadata["global_" + kind], _limit(names[2], names[3]))
    from .admission import admission_snapshot
    admission = admission_snapshot(project)
    work = admission["outstanding"]
    row("Outstanding jobs", work["project"]["used"], work["project"]["limit"],
        work["installation"]["used"], work["installation"]["limit"])
    for operation, label in [("create", "Job creation burst"), ("execute", "Execution request burst")]:
        capacity = admission["rates"][operation]
        project_rate, global_rate = capacity["project"], capacity["installation"]
        row(label, project_rate["limit"] - project_rate["remaining"], project_rate["limit"],
            global_rate["limit"] - global_rate["remaining"], global_rate["limit"])
        rows[-1].update(retry_after_seconds=max(project_rate["retry_after_seconds"], global_rate["retry_after_seconds"]),
            project_per_minute=project_rate["per_minute"], global_per_minute=global_rate["per_minute"])
    return rows


def idempotency_charge_id(operation, key):
    return hashlib.sha256((operation + "\0" + key).encode()).hexdigest()


def compact_idempotency(*, before, limit=1000):
    """Discard response bodies, retaining identity/hash forever to prevent replay."""
    count = 0
    identifiers = list(IdempotencyRecord.objects.filter(compacted=False, created_at__lt=before).order_by(
        "created_at", "id").values_list("id", flat=True)[:min(max(int(limit), 0), 1000)])
    for identifier in identifiers:
        with transaction.atomic():
            row = IdempotencyRecord.objects.select_related("project").get(id=identifier)
            _lock(row.project)
            row = IdempotencyRecord.objects.select_for_update().get(id=identifier)
            if row.compacted:
                continue
            row.response = {}
            row.status, row.compacted = 409, True
            row.save(update_fields=["response", "status", "compacted"])
            # Compaction must still work when admissions have filled all quotas.
            MetadataUsage.objects.filter(project=row.project, kind="idempotency",
                object_id=idempotency_charge_id(row.operation, row.key)).update(size_bytes=512 + json_size(row.response))
            count += 1
    return count


def append_audit(project, actor, action, obj, details):
    """A bounded rolling audit is supplementary; attempts/ledger remain durable."""
    with transaction.atomic():
        if project is not None:
            Project.objects.select_for_update().get(id=project.id)
        MetadataPool.objects.get_or_create(id=1)
        MetadataPool.objects.select_for_update().get(id=1)
        if json_size(details) > 2048:
            details = {"truncated": True, "sha256": hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()}
        if project is not None:
            keep = _limit("MAX_PROJECT_AUDIT_EVENTS", 2000)
            cutoff = list(AuditEvent.objects.filter(project=project).order_by("-id").values_list("id", flat=True)[max(keep - 1, 0):max(keep, 1)])
            if cutoff:
                AuditEvent.objects.filter(project=project, id__lte=cutoff[0]).delete()
        keep = _limit("MAX_INSTALLATION_AUDIT_EVENTS", 20000)
        cutoff = list(AuditEvent.objects.order_by("-id").values_list("id", flat=True)[max(keep - 1, 0):max(keep, 1)])
        if cutoff:
            AuditEvent.objects.filter(id__lte=cutoff[0]).delete()
        AuditEvent.objects.create(project=project, actor=str(actor)[:100], action=str(action)[:100],
                                  object_id=str(obj)[:80], details=details)
