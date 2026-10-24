"""Exclusive object publication and recoverable byte reservations.

The fixed per-project OS lock is never unlinked: recovery cannot race a live
writer, even if its reservation has expired or its process is paused.
"""
import fcntl
import hashlib
import os
import shutil
import stat
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .errors import BrokerError
from .models import Blob, Project, StoragePool, UploadGrant, UploadReservation


def blob_path(blob):
    return settings.ARTIFACT_ROOT / str(blob.project_id) / blob.sha256


def read_blob(blob):
    fd = os.open(blob_path(blob), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != blob.size_bytes:
            raise BrokerError("OBJECT_INTEGRITY", "Stored object failed verification.", 409)
        data = handle.read(blob.size_bytes + 1)
    if len(data) != blob.size_bytes or hashlib.sha256(data).hexdigest() != blob.sha256:
        raise BrokerError("OBJECT_INTEGRITY", "Stored object failed verification.", 409)
    return data


@contextmanager
def _writer_lock(project_id):
    settings.ARTIFACT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    locks = settings.ARTIFACT_ROOT / ".upload-locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(locks / str(project_id), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise BrokerError("STORAGE_UNAVAILABLE", "Upload ownership lock is unavailable.", 503)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BrokerError("UPLOAD_BUSY", "Another upload is publishing; retry shortly.", 409) from None
        yield
    finally:
        os.close(fd)


def _finish(reservation, *, abort_if_missing):
    with transaction.atomic():
        current = Project.objects.select_for_update().get(id=reservation.project_id)
        StoragePool.objects.get_or_create(id=1)
        pool = StoragePool.objects.select_for_update().get(id=1)
        upload = UploadReservation.objects.select_for_update().get(id=reservation.id)
        if upload.state != "reserved":
            return Blob.objects.filter(project=current, sha256=upload.sha256).first()
        # This exact temporary name belongs to this durable reservation. Remove
        # its link first so publication interrupted before unlink is recoverable.
        (settings.ARTIFACT_ROOT / str(upload.project_id) / ("upload-" + str(upload.id))).unlink(missing_ok=True)
        candidate = Blob(project=current, sha256=upload.sha256, size_bytes=upload.size_bytes)
        try:
            read_blob(candidate)
        except FileNotFoundError:
            if not abort_if_missing:
                raise BrokerError("OBJECT_INTEGRITY", "Published object is missing.", 409) from None
            blob, created = None, False
        else:
            blob, created = Blob.objects.get_or_create(project=current, sha256=upload.sha256,
                                                       defaults={"size_bytes": upload.size_bytes})
            read_blob(blob)
        current.storage_reserved -= upload.size_bytes
        pool.reserved -= upload.size_bytes
        if created:
            current.storage_used += upload.size_bytes
            current.object_count += 1
            pool.used += upload.size_bytes
        current.save(update_fields=["storage_reserved", "storage_used", "object_count"])
        pool.save(update_fields=["reserved", "used"])
        upload.state = "finalized" if blob is not None else "aborted"
        upload.save(update_fields=["state"])
        # Terminal reservation rows are recovery receipts, never authorization.
        cutoff = list(UploadReservation.objects.exclude(state="reserved").order_by("-created_at", "-id").values_list("created_at", "id")[1000:1001])
        if cutoff:
            instant, identifier = cutoff[0]
            UploadReservation.objects.exclude(state="reserved").filter(
                Q(created_at__lt=instant) | Q(created_at=instant, id__lte=identifier)).delete()
        return blob


def reconcile_uploads(*, limit=32, now=None):
    """Recover expired writers with a fenced, rotating, bounded retry batch.

After initial expiry, expires_at also records next recovery eligibility. Failed
and busy entries move behind older eligible work, including across restarts.
"""
    now = now or timezone.now()
    rows = list(UploadReservation.objects.filter(state="reserved", expires_at__lte=now).exclude(
        sha256="").order_by("expires_at", "created_at", "id")[:min(max(int(limit), 0), 1000)])
    result = {"recovered": 0, "busy": 0, "failed": 0}
    for row in rows:
        try:
            with _writer_lock(row.project_id):
                row.refresh_from_db()
                if row.state == "reserved" and row.expires_at <= now:
                    _finish(row, abort_if_missing=True)
                    result["recovered"] += 1
        except BrokerError as exc:
            result["busy" if exc.code == "UPLOAD_BUSY" else "failed"] += 1
            _defer_recovery(row, now)
        except OSError:
            result["failed"] += 1
            _defer_recovery(row, now)
    return result


def _defer_recovery(row, now):
    # Compare state/eligibility so this bookkeeping cannot resurrect a finalized
    # upload or overwrite a different recovery pass's decision. No byte balance
    # changes and no unlink happen here; live ownership remains the OS lock.
    UploadReservation.objects.filter(id=row.id, state="reserved", expires_at=row.expires_at).update(
        expires_at=now + timedelta(seconds=30))


def store_uploaded_blob(project, data, *, maximum=None):
    """Only explicit receipt of these bytes establishes an independent grant."""
    blob = store_blob(project, data, maximum=maximum)
    UploadGrant.objects.get_or_create(blob=blob)
    return blob


def store_blob(project, data, *, maximum=None):
    """Internal content storage never creates input authority."""
    maximum = settings.MAX_UPLOAD_BYTES if maximum is None else maximum
    if len(data) > maximum:
        raise BrokerError("UPLOAD_LIMIT", "Object exceeds the configured size limit.", 413)
    digest = hashlib.sha256(data).hexdigest()
    # A bounded retry also recovers expired crashed writers without requiring
    # an unbounded directory scan or waiting for a service restart.
    reconcile_uploads(limit=32)
    with _writer_lock(project.id):
        if shutil.disk_usage(settings.ARTIFACT_ROOT).free < len(data) + 8 * 1024 * 1024:
            raise BrokerError("STORAGE_UNAVAILABLE", "Storage headroom is exhausted.", 503)
        with transaction.atomic():
            current = Project.objects.select_for_update().get(id=project.id)
            StoragePool.objects.get_or_create(id=1)
            pool = StoragePool.objects.select_for_update().get(id=1)
            existing = Blob.objects.filter(project=current, sha256=digest).first()
            if existing:
                read_blob(existing)
                return existing
            pending = UploadReservation.objects.filter(project=current, state="reserved").count()
            if (current.storage_used + current.storage_reserved + len(data) > settings.PROJECT_STORAGE_LIMIT
                    or pool.used + pool.reserved + len(data) > settings.GLOBAL_STORAGE_LIMIT
                    or current.object_count + pending >= settings.MAX_OBJECTS or pending >= 8):
                raise BrokerError("STORAGE_QUOTA", "Project or installation storage quota is exhausted.", 409)
            current.storage_reserved += len(data)
            pool.reserved += len(data)
            current.save(update_fields=["storage_reserved"])
            pool.save(update_fields=["reserved"])
            reservation = UploadReservation.objects.create(project=current, size_bytes=len(data), sha256=digest,
                                                           expires_at=timezone.now() + timedelta(minutes=15))
        directory = settings.ARTIFACT_ROOT / str(project.id)
        temporary = None
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            temporary = directory / ("upload-" + str(reservation.id))
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, directory / digest, follow_symlinks=False)
            except FileExistsError:
                pass
            os.unlink(temporary)
            temporary = None
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return _finish(reservation, abort_if_missing=False)
        except Exception:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
            # A failure after publication must account bytes, never discard them.
            _finish(reservation, abort_if_missing=True)
            raise
