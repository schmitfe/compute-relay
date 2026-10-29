"""Explicit, bounded local reclamation; database intent survives interruption."""

from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import os
import stat

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from compute_relay.remote.filesystem import open_directory
from .auth import human_permission
from .errors import BrokerError
from .management import human_view
from .metadata import append_audit
from .models import Artifact, Blob, InputSource, Job, JobFile, Project, StoragePool, UploadGrant, UploadReservation
from .storage import _writer_lock

BATCH_SIZE = 100
SALT = "compute-relay.local-artifact-cleanup"


def candidates(project, job=None):
    artifacts = Artifact.objects.filter(blob_id=OuterRef("pk"))
    unsafe = artifacts.exclude(attempt__state__in=["succeeded", "failed", "cancelled", "expired"],
        attempt__accounting_state__in=["settled", "released"], attempt__collection_pending=False,
        attempt__collection_state__in=["complete", "cleaned"], attempt__job__extension_name="").union(
            artifacts.filter(Q(attempt__lease_expires_at__gt=timezone.now()) | ~Q(attempt__job__project=project)))
    # Extension provenance pins its source job, including workflow result edges.
    pinned_jobs = Q()
    for relation in Job._meta.related_objects:
        if relation.related_model._meta.app_label != "broker":
            pinned_jobs |= Q(attempt__job_id__in=relation.related_model.objects.exclude(
                **{relation.field.name + "__isnull": True}).values(relation.field.name))
    query = Blob.objects.filter(project=project).annotate(
        has_artifact=Exists(artifacts), unsafe=Exists(unsafe),
        has_job_input=Exists(JobFile.objects.filter(blob_id=OuterRef("pk"))),
        has_definition_input=Exists(InputSource.objects.filter(blob_id=OuterRef("pk"))),
        has_upload=Exists(UploadGrant.objects.filter(blob_id=OuterRef("pk"))),
        has_transfer=Exists(UploadReservation.objects.filter(project=project, sha256=OuterRef("sha256"), state="reserved")))
    query = query.filter(unsafe=False, has_job_input=False, has_definition_input=False,
        has_upload=False, has_transfer=False).filter(
            Q(has_artifact=True) | Q(cleanup_pending=True) | Q(created_at__lt=timezone.now() - timedelta(days=1)))
    if pinned_jobs:
        query = query.annotate(pinned=Exists(artifacts.filter(pinned_jobs))).filter(pinned=False)
    if job is not None:
        receipts = job.attempts.exclude(log_state__cleanup__isnull=True).values_list("log_state", flat=True)
        pending = {entry["sha256"] for state in receipts for entry in state.get("cleanup", {}).get("outputs", [])}
        query = query.annotate(other_job=Exists(artifacts.exclude(attempt__job=job))).filter(other_job=False).filter(
            Q(artifact__attempt__job=job) | Q(cleanup_pending=True, sha256__in=pending)).distinct()
    return query.order_by("-cleanup_pending", "created_at", "id")


@contextmanager
def object_directory(project):
    root = open_directory(str(settings.ARTIFACT_ROOT))
    directory = None
    try:
        info = os.fstat(root)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BrokerError("CLEANUP_ROOT_UNSAFE", "The object directory must be private and owned by this service.", 409)
        directory = open_directory(str(project.id), parent=root)
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BrokerError("CLEANUP_ROOT_UNSAFE", "The project object directory must be private and owned by this service.", 409)
        yield directory
    finally:
        if directory is not None:
            os.close(directory)
        os.close(root)


def file_identity(directory, blob):
    if len(blob.sha256) != 64 or any(char not in "0123456789abcdef" for char in blob.sha256):
        raise BrokerError("CLEANUP_OBJECT_UNSAFE", "The stored object identifier is invalid.", 409)
    try:
        info = os.stat(blob.sha256, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_size != blob.size_bytes):
        raise BrokerError("CLEANUP_OBJECT_UNSAFE", "An object changed or is not a private regular file; cleanup has been refused.", 409)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def preview(project, job=None):
    blobs = list(candidates(project, job)[:BATCH_SIZE + 1])
    rows = []
    if blobs:
        try:
            with object_directory(project) as directory:
                for blob in blobs[:BATCH_SIZE]:
                    identity = file_identity(directory, blob)
                    outputs = list(Artifact.objects.filter(blob=blob).order_by("id").values_list(
                        "id", "attempt_id", "path"))
                    rows.append({"id": str(blob.id), "sha256": blob.sha256, "size_bytes": blob.size_bytes,
                        "disk_bytes": identity[2] if identity else 0, "identity": identity,
                        "pending": blob.cleanup_pending,
                        "outputs": [[str(identifier), str(attempt), path] for identifier, attempt, path in outputs]})
        except OSError as exc:
            raise BrokerError("CLEANUP_ROOT_UNSAFE", "The object directory is unavailable or contains an unsafe link.", 409) from exc
    return {"rows": rows, "bytes": sum(row["disk_bytes"] for row in rows),
        "accounted_bytes": sum(row["size_bytes"] for row in rows), "more": len(blobs) > BATCH_SIZE}


def preview_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cleanup(project, actor, token, job=None):
    if project.owner_id != actor.id:
        raise BrokerError("SCOPE_DENIED", "Only the project owner can request local cleanup.", 403)
    try:
        approved = signing.loads(token, salt=SALT, max_age=600)
    except (signing.BadSignature, TypeError):
        raise BrokerError("CLEANUP_PREVIEW_EXPIRED", "Open a fresh cleanup preview before deleting files.", 409) from None
    scope = {"project": str(project.id), "job": str(job.id) if job else None, "actor": str(actor.id)}
    if any(approved.get(key) != value for key, value in scope.items()):
        raise BrokerError("CLEANUP_PREVIEW_CHANGED", "This confirmation belongs to another cleanup preview.", 409)
    with _writer_lock(project.id):
        with transaction.atomic():
            project = Project.objects.select_for_update().get(id=project.id, owner=actor)
            current = preview(project, job)
            if approved.get("digest") != preview_digest(current):
                raise BrokerError("CLEANUP_PREVIEW_CHANGED", "Files or their references changed. Review a fresh cleanup preview.", 409)
            if not current["rows"]:
                return {"objects": 0, "bytes": 0}
            for row in current["rows"]:
                blob = Blob.objects.get(id=row["id"], project=project)
                artifacts = list(Artifact.objects.filter(blob=blob).select_related("attempt"))
                attempts = {artifact.attempt_id: artifact.attempt for artifact in artifacts}
                for attempt in attempts.values():
                    receipt = attempt.log_state.setdefault("cleanup", {"outputs": []})
                    paths = {artifact.path for artifact in artifacts if artifact.attempt_id == attempt.id}
                    pending = Blob.objects.filter(project=project, cleanup_pending=True).values_list("sha256", flat=True)
                    receipt["outputs"] = [entry for entry in receipt["outputs"]
                        if entry["path"] not in paths or entry["sha256"] in pending]
                    receipt["outputs"].extend({"path": path, "sha256": blob.sha256, "size_bytes": blob.size_bytes} for path in sorted(paths))
                    attempt.collection_state = "cleaned"
                    attempt.save(update_fields=["log_state", "collection_state"])
                Artifact.objects.filter(blob=blob).delete()
                blob.cleanup_pending = True
                blob.save(update_fields=["cleanup_pending"])
        # Commit the loss of artifact authority before unlinking. Interrupted
        # cleanup keeps charged bytes and a retryable Blob intent, never grants.
        reclaimed, deleted, skipped = 0, 0, 0
        with object_directory(project) as directory:
            for row in current["rows"]:
                with transaction.atomic():
                    project = Project.objects.select_for_update().get(id=project.id, owner=actor)
                    StoragePool.objects.get_or_create(id=1)
                    pool = StoragePool.objects.select_for_update().get(id=1)
                    # Fence FK insertion and take SQLite's writer lock before unlinking.
                    Blob.objects.filter(id=row["id"], project=project, cleanup_pending=True).update(cleanup_pending=True)
                    blob = Blob.objects.get(id=row["id"], project=project, cleanup_pending=True)
                    if any(model.objects.filter(blob=blob).exists() for model in (Artifact, JobFile, InputSource, UploadGrant)):
                        blob.cleanup_pending = False
                        blob.save(update_fields=["cleanup_pending"])
                        skipped += 1
                        continue
                    if file_identity(directory, blob) != row["identity"]:
                        raise BrokerError("CLEANUP_OBJECT_CHANGED", "An object changed during cleanup. Keep it for inspection.", 409)
                    if row["identity"] is not None:
                        os.unlink(blob.sha256, dir_fd=directory)
                        os.fsync(directory)
                    project.storage_used -= blob.size_bytes
                    project.object_count -= 1
                    pool.used -= blob.size_bytes
                    project.save(update_fields=["storage_used", "object_count"])
                    pool.save(update_fields=["used"])
                    blob.delete()
                    reclaimed += row["disk_bytes"]
                    deleted += 1
            append_audit(project, actor.id, "artifacts.local_cleaned", job.id if job else project.id,
                {"objects": deleted, "bytes": reclaimed, "newly_referenced": skipped})
        return {"objects": deleted, "bytes": reclaimed, "newly_referenced": skipped}


@human_view
def portal(request, project_id=None, job_id=None):
    job = get_object_or_404(Job.objects.select_related("project"), id=job_id) if job_id else None
    project = job.project if job else get_object_or_404(Project, id=project_id)
    human_permission(request.user, project, "project_admin")
    from . import remote_cleanup
    if request.method == "POST":
        if request.POST.get("confirm") != "delete":
            raise BrokerError("CLEANUP_CONFIRMATION_REQUIRED", "Confirm deletion after reviewing the cleanup preview.")
        scope = request.POST.get("cleanup_scope", "local")
        if scope == "remote":
            remote_cleanup.queue(project, request.user, request.POST.get("preview_token"), job)
            return redirect(request.path + "?remote_queued=1")
        if scope != "local":
            raise BrokerError("INVALID_REQUEST", "Choose local files or remote attempt directories.")
        try:
            result = cleanup(project, request.user, request.POST.get("preview_token"), job)
        except OSError as exc:
            raise BrokerError("CLEANUP_INTERRUPTED", "Cleanup was interrupted. Its remaining bytes stay charged; open a fresh preview to resume.", 503) from exc
        return redirect(request.path + "?reclaimed=" + str(result["bytes"]))
    value = preview(project, job)
    token = signing.dumps({"project": str(project.id), "job": str(job.id) if job else None,
        "actor": str(request.user.id), "digest": preview_digest(value)}, salt=SALT)
    remote = remote_cleanup.preview(project, job)
    return render(request, "broker/artifact_cleanup.html", {"project": project, "job": job,
        "cleanup": value, "preview_token": token, "completed": "reclaimed" in request.GET,
        "remote_cleanup": remote, "remote_token": remote_cleanup.confirmation(project, request.user, remote, job),
        "remote_queued": "remote_queued" in request.GET})
