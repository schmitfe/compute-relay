"""Small bounded uploads with persistent project/global reservations."""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import transaction

from .errors import BrokerError
from .models import Blob, Project, StoragePool, UploadReservation


def blob_path(blob):
    return settings.ARTIFACT_ROOT / str(blob.project_id) / blob.sha256


def read_blob(blob):
    path = blob_path(blob)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(blob.size_bytes + 1)
    if len(data) != blob.size_bytes or hashlib.sha256(data).hexdigest() != blob.sha256:
        raise BrokerError("OBJECT_INTEGRITY", "Stored object failed verification.", 409)
    return data


def store_blob(project, data, *, maximum=None):
    maximum = settings.MAX_UPLOAD_BYTES if maximum is None else maximum
    if len(data) > maximum:
        raise BrokerError("UPLOAD_LIMIT", "Object exceeds the configured size limit.", 413)
    digest = hashlib.sha256(data).hexdigest()
    settings.ARTIFACT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(settings.ARTIFACT_ROOT).free < len(data) + 8 * 1024 * 1024:
        raise BrokerError("STORAGE_UNAVAILABLE", "Storage headroom is exhausted.", 503)
    with transaction.atomic():
        StoragePool.objects.get_or_create(id=1)
        pool = StoragePool.objects.select_for_update().get(id=1)
        current = Project.objects.select_for_update().get(id=project.id)
        existing = Blob.objects.filter(project=current, sha256=digest).first()
        if existing:
            read_blob(existing)
            return existing
        pending = UploadReservation.objects.filter(project=current, state="reserved").count()
        if current.storage_used + current.storage_reserved + len(data) > settings.PROJECT_STORAGE_LIMIT or pool.used + pool.reserved + len(data) > settings.GLOBAL_STORAGE_LIMIT or current.object_count + pending >= settings.MAX_OBJECTS or pending >= 8:
            raise BrokerError("STORAGE_QUOTA", "Project or installation storage quota is exhausted.", 409)
        current.storage_reserved += len(data)
        pool.reserved += len(data)
        current.save(update_fields=["storage_reserved"])
        pool.save(update_fields=["reserved"])
        reservation = UploadReservation.objects.create(project=current, size_bytes=len(data))
    directory = settings.ARTIFACT_ROOT / str(project.id)
    directory.mkdir(mode=0o700, exist_ok=True)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix="upload-", dir=directory)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        destination = directory / digest
        # Private directory; hard-link publication is exclusive and cannot follow a symlink.
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            pass
        os.unlink(temporary)
        temporary = None
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        with transaction.atomic():
            pool = StoragePool.objects.select_for_update().get(id=1)
            current = Project.objects.select_for_update().get(id=project.id)
            upload = UploadReservation.objects.select_for_update().get(id=reservation.id)
            blob, created = Blob.objects.get_or_create(project=current, sha256=digest, defaults={"size_bytes": len(data)})
            read_blob(blob)
            current.storage_reserved -= len(data)
            pool.reserved -= len(data)
            if created:
                current.storage_used += len(data)
                current.object_count += 1
                pool.used += len(data)
            current.save(update_fields=["storage_reserved", "storage_used", "object_count"])
            pool.save(update_fields=["reserved", "used"])
            upload.state = "finalized"
            upload.save(update_fields=["state"])
            return blob
    except Exception:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
        with transaction.atomic():
            pool = StoragePool.objects.select_for_update().get(id=1)
            current = Project.objects.select_for_update().get(id=project.id)
            upload = UploadReservation.objects.select_for_update().get(id=reservation.id)
            if upload.state == "reserved":
                current.storage_reserved -= len(data)
                pool.reserved -= len(data)
                current.save(update_fields=["storage_reserved"])
                pool.save(update_fields=["reserved"])
                upload.state = "aborted"
                upload.save(update_fields=["state"])
        raise
