"""Exclusive object publication and recoverable byte reservations.

The fixed per-project OS lock is never unlinked: recovery cannot race a live
writer, even if its reservation has expired or its process is paused.
"""
import fcntl
import hashlib
import os
import shutil
import stat
import threading
from collections import OrderedDict
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .errors import BrokerError
from .models import Blob, Project, StoragePool, UploadGrant, UploadReservation
from compute_relay.transfer import CHUNK_BYTES, fingerprint

MAX_PENDING_UPLOADS = 8


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


_verified_objects = OrderedDict()
_verification_lock = threading.Lock()


def _verification_key(blob, info):
    return (str(blob.project_id), blob.sha256, blob.size_bytes, info.st_dev, info.st_ino,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _remember_verified(key):
    with _verification_lock:
        _verified_objects[key] = True
        _verified_objects.move_to_end(key)
        while len(_verified_objects) > 4096:
            _verified_objects.popitem(last=False)


def open_verified_blob(blob):
    """Reuse hashes only while descriptor identity and metadata are unchanged.

    This bounded process cache is invalidated by replacement, writes or metadata
    changes. A restart verifies again. Private object files are immutable under
    the supported account boundary.
    """
    fd = os.open(blob_path(blob), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(fd, "rb")
    try:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != blob.size_bytes:
            raise BrokerError("OBJECT_INTEGRITY", "Stored object failed verification.", 409)
        key = _verification_key(blob, info)
        with _verification_lock:
            cached = key in _verified_objects
        if not cached:
            digest, size = fingerprint(stream)
            if (digest != blob.sha256 or size != blob.size_bytes
                    or key != _verification_key(blob, os.fstat(stream.fileno()))):
                raise BrokerError("OBJECT_INTEGRITY", "Stored object failed verification.", 409)
        _remember_verified(key)
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise


def verify_blob(blob):
    """Verify arbitrarily large retained objects without buffering them."""
    with open_verified_blob(blob):
        pass


def blob_chunk(blob, offset, limit=CHUNK_BYTES):
    if type(offset) is not int or not 0 <= offset <= blob.size_bytes or not 1 <= limit <= CHUNK_BYTES:
        raise BrokerError("INVALID_RANGE", "Invalid object range.", 416)
    fd = os.open(blob_path(blob), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != blob.size_bytes:
            raise BrokerError("OBJECT_INTEGRITY", "Stored object failed verification.", 409)
        stream.seek(offset)
        return stream.read(min(limit, blob.size_bytes - offset))


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


def _finish(reservation, *, abort_if_missing, abort=False):
    # All callers hold the project writer lock. Hashing a multi-GiB object must
    # happen outside SQLite's write transaction so other projects can progress.
    snapshot = UploadReservation.objects.get(id=reservation.id)
    verified = False
    if snapshot.state == "reserved":
        (settings.ARTIFACT_ROOT / str(snapshot.project_id) / ("upload-" + str(snapshot.id))).unlink(missing_ok=True)
        candidate = Blob(project_id=snapshot.project_id, sha256=snapshot.sha256, size_bytes=snapshot.size_bytes)
        try:
            if abort or (snapshot.binding and not snapshot.verified):
                raise FileNotFoundError("Unverified resumable transfer")
            verify_blob(candidate)
        except FileNotFoundError:
            if not abort_if_missing:
                raise BrokerError("OBJECT_INTEGRITY", "Published object is missing.", 409) from None
        else:
            verified = True
    with transaction.atomic():
        current = Project.objects.select_for_update().get(id=reservation.project_id)
        StoragePool.objects.get_or_create(id=1)
        pool = StoragePool.objects.select_for_update().get(id=1)
        upload = UploadReservation.objects.select_for_update().get(id=reservation.id)
        if upload.state != "reserved":
            return Blob.objects.filter(project=current, sha256=upload.sha256).first()
        if not verified:
            blob, created = None, False
        else:
            blob, created = Blob.objects.get_or_create(project=current, sha256=upload.sha256,
                                                       defaults={"size_bytes": upload.size_bytes})
            if blob.size_bytes != upload.size_bytes:
                raise BrokerError("OBJECT_INTEGRITY", "Stored object failed verification.", 409)
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
                    or current.object_count + pending >= settings.MAX_OBJECTS or pending >= MAX_PENDING_UPLOADS):
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


def begin_transfer(project, digest, size, binding, *, uploaded=False):
    """Reserve the complete object once; retries recover the same private partial."""
    import re
    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(size) is not int or not 0 <= size <= settings.MAX_TRANSFER_BYTES):
        raise BrokerError("TRANSFER_LIMIT", "Invalid transfer hash or byte count.", 413)
    reconcile_uploads(limit=8)
    with _writer_lock(project.id):
        existing = Blob.objects.filter(project=project, sha256=digest, size_bytes=size).first()
        current = UploadReservation.objects.filter(project=project, sha256=digest, size_bytes=size,
            binding=binding, state="reserved").first()
        # A hash-only request must never turn a retained output into an upload grant.
        if existing and (not uploaded or UploadGrant.objects.filter(blob=existing).exists()):
            verify_blob(existing)
            if current:
                _finish(current, abort_if_missing=True, abort=True)
            return {"complete": True, "sha256": digest, "size_bytes": size, "offset": size}
        if current:
            partial = settings.ARTIFACT_ROOT / str(project.id) / ("upload-" + str(current.id))
            fd = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > size:
                    raise BrokerError("OBJECT_INTEGRITY", "Transfer partial failed verification.", 409)
                offset = info.st_size
            finally:
                os.close(fd)
            return {"complete": False, "transfer_id": str(current.id), "offset": offset}
        if shutil.disk_usage(settings.ARTIFACT_ROOT).free < size + 8 * 1024 * 1024:
            raise BrokerError("STORAGE_UNAVAILABLE", "Storage headroom is exhausted.", 503)
        with transaction.atomic():
            current = Project.objects.select_for_update().get(id=project.id)
            StoragePool.objects.get_or_create(id=1)
            pool = StoragePool.objects.select_for_update().get(id=1)
            pending = UploadReservation.objects.filter(project=project, state="reserved").count()
            if (current.storage_used + current.storage_reserved + size > settings.PROJECT_STORAGE_LIMIT
                    or pool.used + pool.reserved + size > settings.GLOBAL_STORAGE_LIMIT
                    or current.object_count + pending >= settings.MAX_OBJECTS or pending >= MAX_PENDING_UPLOADS):
                raise BrokerError("STORAGE_QUOTA", "Transfer exceeds the storage quota.", 409)
            current.storage_reserved += size
            pool.reserved += size
            current.save(update_fields=["storage_reserved"])
            pool.save(update_fields=["reserved"])
            reservation = UploadReservation.objects.create(project=project, size_bytes=size, sha256=digest,
                binding=binding, expires_at=timezone.now() + timedelta(hours=24))
        directory = settings.ARTIFACT_ROOT / str(project.id)
        directory.mkdir(mode=0o700, exist_ok=True)
        try:
            fd = os.open(directory / ("upload-" + str(reservation.id)),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except Exception:
            _finish(reservation, abort_if_missing=True)
            raise
        return {"complete": False, "transfer_id": str(reservation.id), "offset": 0}


def transfer_chunk(project, transfer_id, binding, offset, content, *, finish=False, uploaded=False):
    if type(offset) is not int or offset < 0 or not isinstance(content, bytes) or len(content) > CHUNK_BYTES:
        raise BrokerError("INVALID_RANGE", "Invalid transfer chunk.")
    with _writer_lock(project.id):
        reservation = UploadReservation.objects.get(id=transfer_id, project=project, binding=binding)
        if reservation.state == "finalized":
            if uploaded:
                if not reservation.verified:
                    raise BrokerError("TRANSFER_UNVERIFIED", "Independent upload bytes were not verified.", 409)
                blob = Blob.objects.get(project=project, sha256=reservation.sha256, size_bytes=reservation.size_bytes)
                UploadGrant.objects.get_or_create(blob=blob)
            return {"complete": True, "sha256": reservation.sha256, "size_bytes": reservation.size_bytes,
                    "offset": reservation.size_bytes}
        if reservation.state != "reserved" or reservation.expires_at <= timezone.now():
            raise BrokerError("TRANSFER_EXPIRED", "Restart the expired transfer.", 409)
        path = settings.ARTIFACT_ROOT / str(project.id) / ("upload-" + str(reservation.id))
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r+b") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise BrokerError("OBJECT_INTEGRITY", "Transfer partial failed verification.", 409)
            if offset > info.st_size or offset + len(content) > reservation.size_bytes:
                raise BrokerError("TRANSFER_OFFSET", "Resume from the acknowledged offset.", 409)
            if offset < info.st_size:
                stream.seek(offset)
                if stream.read(len(content)) != content:
                    raise BrokerError("TRANSFER_CONFLICT", "Retried chunk differs from received bytes.", 409)
            else:
                stream.seek(offset)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            size = os.fstat(stream.fileno()).st_size
            if finish:
                stream.seek(0)
                before_hash = os.fstat(stream.fileno())
                digest, measured = fingerprint(stream)
                verified_info = os.fstat(stream.fileno())
                candidate = Blob(project=project, sha256=reservation.sha256, size_bytes=reservation.size_bytes)
                if (measured != reservation.size_bytes or digest != reservation.sha256
                        or _verification_key(candidate, before_hash) != _verification_key(candidate, verified_info)):
                    # A full corrupt partial cannot be resumed. Release its
                    # reservation immediately, including when the target hash
                    # already exists without independent upload authority.
                    _finish(reservation, abort_if_missing=True, abort=True)
                    raise BrokerError("OBJECT_INTEGRITY", "Transferred object failed checksum verification.", 409)
        UploadReservation.objects.filter(id=reservation.id).update(
            expires_at=timezone.now() + timedelta(hours=24), verified=finish)
        if not finish:
            return {"complete": False, "transfer_id": str(reservation.id), "offset": size}
        target = path.parent / reservation.sha256
        published = False
        try:
            os.link(path, target, follow_symlinks=False)
            published = True
        except FileExistsError:
            pass
        path.unlink()
        if published:
            # Link/unlink changes ctime; cache the final metadata only for the
            # inode just verified and published by this writer.
            candidate = Blob(project=project, sha256=reservation.sha256, size_bytes=reservation.size_bytes)
            info = target.stat(follow_symlinks=False)
            if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    == (verified_info.st_dev, verified_info.st_ino, verified_info.st_size, verified_info.st_mtime_ns)):
                _remember_verified(_verification_key(candidate, info))
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        blob = _finish(reservation, abort_if_missing=False)
        if uploaded:
            UploadGrant.objects.get_or_create(blob=blob)
        return {"complete": True, "sha256": blob.sha256, "size_bytes": blob.size_bytes, "offset": blob.size_bytes}
