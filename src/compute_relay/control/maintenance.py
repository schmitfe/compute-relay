"""Bounded metadata compaction; durable execution identities are never deleted."""

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .metadata import append_audit, json_size, retire_metadata
from .models import Job, Project


def compact_draft_jobs(*, owner, before, limit=100, apply=False):
    if timezone.is_naive(before) or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Use an aware cutoff and a batch limit from 1 through 1000")
    candidates = Job.objects.filter(project__owner=owner, created_at__lt=before,
        state__in=["draft", "prepared"], extension_name="", execution_requests__isnull=True).annotate(
            has_active_child=Exists(Job.objects.filter(previous_id=OuterRef("pk")).exclude(state="archived"))
        ).filter(has_active_child=False)
    identifiers = list(candidates.order_by("created_at", "id").values_list("id", flat=True)[:limit])
    result = {"candidates": 0, "archived": 0, "skipped": 0}
    for identifier in identifiers:
        with transaction.atomic():
            project_id = Job.objects.values_list("project_id", flat=True).get(id=identifier)
            project = Project.objects.select_for_update().get(id=project_id, owner=owner)
            job = Job.objects.select_for_update().get(id=identifier, project=project)
            if (job.state not in {"draft", "prepared"} or job.extension_name or job.created_at >= before
                    or job.execution_requests.exists() or Job.objects.filter(previous_id=job.id).exclude(state="archived").exists()):
                result["skipped"] += 1
                continue
            result["candidates"] += 1
            if not apply:
                continue
            name = job.name[:100]
            retained_request = {"name": name, "connection_id": job.request["connection_id"]}
            tombstone = {**retained_request, "manifest_sha256": job.manifest_sha256}
            # Both changes share the transaction: compaction can free capacity
            # even at its byte ceiling, but a full tombstone cap rolls it back.
            retire_metadata(project, job.id, 512 + json_size(tombstone))
            job.files.all().delete()
            job.request, job.manifest, job.state = retained_request, None, "archived"
            job.save(update_fields=["request", "manifest", "state"])
            append_audit(project, owner.id, "job.metadata_archived", job.id,
                {"manifest_sha256": job.manifest_sha256})
            result["archived"] += 1
    return result
