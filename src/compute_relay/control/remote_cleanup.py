"""Human-confirmed remote cleanup; fixed worker leases own every remote operation."""

import hashlib
import json
import posixpath

from django.apps import apps
from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .errors import BrokerError
from .metadata import append_audit
from .models import Attempt, Connection, Environment, InputSource, Job, JobFile, Project, UploadReservation

SALT = "compute-relay.remote-attempt-cleanup"


def equivalent_connections(connection):
    """Same owned SSH destination, even when another connector/profile names it."""
    from .connection_identity import public_ssh_identity
    identity = public_ssh_identity(connection.configuration)
    if identity is None:
        return [connection.id]
    identity["host"] = identity["host"].lower().rstrip(".")
    identifiers = []
    for identifier, configuration in Connection.objects.filter(owner_id=connection.owner_id,
            backend=connection.backend).values_list("id", "configuration"):
        other = public_ssh_identity(configuration)
        if other:
            other["host"] = other["host"].lower().rstrip(".")
            if other == identity:
                identifiers.append(identifier)
    # Host aliases and different mount paths require operator knowledge; no DNS
    # lookup can establish that two remote filesystems are the same.
    return identifiers


def environment_paths(configuration):
    return [configuration.get(key) for key in ("prefix", "interpreter", "conda_executable", "module_init")] + list(
        configuration.get("module_paths", []))


def inside(directory, path):
    return (isinstance(directory, str) and directory.startswith("/") and isinstance(path, str)
        and path.startswith("/") and posixpath.commonpath([posixpath.normpath(directory), posixpath.normpath(path)])
        == posixpath.normpath(directory))


def protect_registration(connection, paths, *, environment=False):
    """Caller holds the owner's lock, also used by final cleanup dispatch."""
    for identity in Attempt.objects.filter(connection_id__in=equivalent_connections(connection),
            remote_cleanup__state__in=["queued", "running"]).values_list("native_identity", flat=True):
        directory = (identity or {}).get("work_dir")
        if any(inside(directory, path) or environment and inside(path, directory) for path in paths):
            raise BrokerError("CLEANUP_IN_PROGRESS",
                "Remote cleanup is queued or running for this directory. Wait for it to finish and choose a retained location.", 409)


def blocked(attempt, *, leased=False):
    if attempt.connection.backend != "slurm" or attempt.job.extension_name:
        return "Only ordinary Slurm attempt directories are supported."
    if (attempt.state not in {"succeeded", "failed", "cancelled", "expired"}
            or attempt.accounting_state not in {"settled", "released"}
            or not attempt.native_identity or attempt.collection_pending
            or attempt.collection_state not in {"complete", "cleaned"} or attempt.cancel_requested):
        return "Execution, accounting and output collection must be resolved."
    if not leased and attempt.lease_expires_at and attempt.lease_expires_at > timezone.now():
        return "This attempt still has an active connector operation."
    if (attempt.connection.archived or attempt.connection.worker.revoked
            or not attempt.connection.setup_complete or not attempt.connection.configuration_confirmed):
        return "A current confirmed owned connector is required."
    if (attempt.connection.owner_id != attempt.job.project.owner_id
            or attempt.connection.worker.owner_id != attempt.job.project.owner_id
            or not attempt.job.project.owner.is_active):
        return "The project owner must own the cluster connection and connector."
    if UploadReservation.objects.filter(project=attempt.job.project, state="reserved",
            binding__startswith="output:" + str(attempt.id) + ":").exists():
        return "An output transfer is still pending."
    if (JobFile.objects.filter(blob__artifact__attempt=attempt).exists()
            or InputSource.objects.filter(blob__artifact__attempt=attempt).exists()):
        return "A result is referenced by another job or module definition."
    if attempt.job.manifest.get("connection_revision_sha256") != attempt.connection.revision_sha256:
        return "The cluster configuration changed; this old attempt requires manual remote cleanup."
    directory = attempt.native_identity.get("work_dir")
    connections = equivalent_connections(attempt.connection)
    for config in Environment.objects.filter(connection_id__in=connections).values_list("configuration", flat=True):
        if any(inside(directory, path) or inside(path, directory) for path in environment_paths(config)):
            return "A registered environment protects this directory."
    if apps.is_installed("compute_relay.extensions.storage"):
        from compute_relay.extensions.storage.models import StorageCapacityPool, StorageTarget, StorageTargetRegistration
        roots = list(StorageTarget.objects.filter(connection_id__in=connections).values_list("root", flat=True))
        roots.extend(StorageCapacityPool.objects.filter(connection_id__in=connections).values_list("probe_root", flat=True))
        from compute_relay.extensions.storage.services import confirmed_directory_root
        for registration in StorageTargetRegistration.objects.filter(connection_id__in=connections,
                task__state__in=["queued", "running", "succeeded"], task__expires_at__gt=timezone.now()).select_related("task"):
            roots.extend([registration.root, confirmed_directory_root(registration)])
        if any(inside(directory, root) for root in roots):
            return "A registered storage location protects this directory."
    for relation in Job._meta.related_objects:
        if (relation.related_model._meta.app_label != "broker"
                and relation.related_model.objects.filter(**{relation.field.name: attempt.job}).exists()):
            return "A module retains this job as provenance."
    return ""


def preview(project, job=None):
    attempts = Attempt.objects.filter(job__project=project).select_related("job", "connection__worker")
    if job is not None:
        attempts = attempts.filter(job=job)
    attempts = attempts.filter(connection__backend="slurm", job__extension_name="").filter(
        Q(remote_cleanup__state__isnull=True) | ~Q(remote_cleanup__state="complete")).order_by("created_at", "id")
    rows = []
    for attempt in attempts[:101]:
        reason = blocked(attempt)
        if not reason and attempt.remote_cleanup.get("state") in {"queued", "running"}:
            reason = "Cleanup is already queued or running."
        rows.append({"id": str(attempt.id), "job": attempt.job.name, "state": attempt.remote_cleanup.get("state", "not_requested"),
            "eligible": not reason, "reason": reason, "size_bytes": None})
    return {"rows": rows[:100], "more": len(rows) > 100,
        "eligible_count": sum(row["eligible"] for row in rows[:100])}


def confirmation(project, actor, value, job=None):
    return signing.dumps({"project": str(project.id), "job": str(job.id) if job else None, "actor": str(actor.id),
        "digest": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()}, salt=SALT)


def queue(project, actor, token, job=None):
    if project.owner_id != actor.id:
        raise BrokerError("SCOPE_DENIED", "Only the project owner can request remote cleanup.", 403)
    try:
        approved = signing.loads(token, salt=SALT, max_age=600)
    except (signing.BadSignature, TypeError):
        raise BrokerError("CLEANUP_PREVIEW_EXPIRED", "Review a fresh remote cleanup preview.", 409) from None
    scope = {"project": str(project.id), "job": str(job.id) if job else None, "actor": str(actor.id)}
    if any(approved.get(key) != value for key, value in scope.items()):
        raise BrokerError("CLEANUP_PREVIEW_CHANGED", "This remote cleanup preview belongs to another scope.", 409)
    with transaction.atomic():
        project = Project.objects.select_for_update().get(id=project.id, owner=actor)
        current = preview(project, job)
        if approved.get("digest") != hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest():
            raise BrokerError("CLEANUP_PREVIEW_CHANGED", "Remote cleanup references changed. Review a fresh preview.", 409)
        identifiers = [row["id"] for row in current["rows"] if row["eligible"]]
        Attempt.objects.filter(id__in=identifiers, job__project=project).update(
            remote_cleanup={"state": "queued"}, next_poll_at=timezone.now())
        append_audit(project, actor.id, "artifacts.remote_cleanup_queued", job.id if job else project.id,
            {"attempts": len(identifiers)})
    return {"queued": len(identifiers)}


def accept_report(attempt, data):
    if data.get("native_identity") != attempt.native_identity:
        raise BrokerError("NATIVE_IDENTITY_CHANGED", "Remote cleanup must retain its native identity.", 409)
    if data.get("outcome") == "cleaned":
        result = data.get("cleanup", {})
        if (not isinstance(result, dict) or result.get("state") != "complete" or result.get("retained_journal") is not True
                or any(type(result.get(key)) is not int or not 0 <= result[key] <= 2**63 - 1 for key in ("size_bytes", "entries"))
                or result["entries"] > 10000):
            raise BrokerError("INVALID_REPORT", "Invalid remote cleanup receipt.", 409)
        attempt.remote_cleanup = {key: result[key] for key in ("state", "size_bytes", "entries", "retained_journal")}
    else:
        attempt.remote_cleanup = {"state": "failed", "error_code": "REMOTE_CLEANUP_FAILED"}
    attempt.lease_expires_at = None
    attempt.save(update_fields=["remote_cleanup", "lease_expires_at"])
    append_audit(attempt.job.project, attempt.job.project.owner_id, "artifacts.remote_cleanup_reported", attempt.id,
        attempt.remote_cleanup)
    return {"accepted": True, "remote_cleanup": attempt.remote_cleanup}
