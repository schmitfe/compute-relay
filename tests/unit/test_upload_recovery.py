"""Recovery neither leaks reservations on mkdir failure nor releases live writes."""
import hashlib
import os
import stat
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from compute_relay.control.models import Blob, StoragePool, UploadGrant, UploadReservation
from compute_relay.control.errors import BrokerError
from compute_relay.control.storage import _writer_lock, read_blob, reconcile_uploads, store_blob
from test_control import broker


def reserve(context, data, *, expired=True):
    context.project.storage_reserved += len(data)
    context.project.save(update_fields=["storage_reserved"])
    pool, _ = StoragePool.objects.get_or_create(id=1)
    pool.reserved += len(data)
    pool.save(update_fields=["reserved"])
    return UploadReservation.objects.create(project=context.project, size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        expires_at=timezone.now() + timedelta(minutes=-1 if expired else 10))


def test_repeated_destination_mkdir_errors_release_all_reservations(broker, settings, monkeypatch):
    original = Path.mkdir
    def fail(path, *args, **kwargs):
        if path == settings.ARTIFACT_ROOT / str(broker.project.id):
            raise OSError("synthetic directory failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", fail)
    for _ in range(12):
        with pytest.raises(OSError):
            store_blob(broker.project, b"never published")
    broker.project.refresh_from_db()
    assert broker.project.storage_reserved == 0
    assert StoragePool.objects.get(id=1).reserved == 0
    assert not UploadReservation.objects.filter(state="reserved").exists()
    assert not Blob.objects.exists()


def test_expired_reservation_held_by_live_writer_is_not_released(broker):
    row = reserve(broker, b"alive")
    with _writer_lock(broker.project.id):
        assert reconcile_uploads() == {"recovered": 0, "busy": 1, "failed": 0}
    row.refresh_from_db()
    assert row.state == "reserved"
    broker.project.refresh_from_db()
    assert broker.project.storage_reserved == 5


def test_crash_after_publication_finalizes_bytes_and_removes_only_own_temporary(broker, settings):
    data = b"published before process death"
    row = reserve(broker, data)
    directory = settings.ARTIFACT_ROOT / str(broker.project.id)
    directory.mkdir(parents=True)
    temporary = directory / ("upload-" + str(row.id))
    temporary.write_bytes(data)
    os.link(temporary, directory / row.sha256)
    unrelated = directory / "not-an-upload"
    unrelated.write_bytes(b"keep")
    assert reconcile_uploads() == {"recovered": 1, "busy": 0, "failed": 0}
    blob = Blob.objects.get(sha256=row.sha256)
    assert read_blob(blob) == data and not temporary.exists() and unrelated.exists()
    assert not UploadGrant.objects.filter(blob=blob).exists()
    broker.project.refresh_from_db()
    pool = StoragePool.objects.get(id=1)
    assert (broker.project.storage_reserved, broker.project.storage_used, broker.project.object_count) == (0, len(data), 1)
    assert (pool.reserved, pool.used) == (0, len(data))
    assert reconcile_uploads()["recovered"] == 0


def test_dead_unpublished_writer_is_aborted_but_unexpired_writer_is_untouched(broker, settings):
    dead = reserve(broker, b"dead")
    pending = reserve(broker, b"pending", expired=False)
    directory = settings.ARTIFACT_ROOT / str(broker.project.id)
    directory.mkdir(parents=True)
    temporary = directory / ("upload-" + str(dead.id))
    temporary.write_bytes(b"partial")
    assert reconcile_uploads(limit=1)["recovered"] == 1
    dead.refresh_from_db()
    pending.refresh_from_db()
    assert dead.state == "aborted" and pending.state == "reserved" and not temporary.exists()
    broker.project.refresh_from_db()
    assert broker.project.storage_reserved == len(b"pending")


def test_corrupt_published_object_keeps_its_reservation_for_manual_recovery(broker, settings):
    row = reserve(broker, b"expected")
    directory = settings.ARTIFACT_ROOT / str(broker.project.id)
    directory.mkdir(parents=True)
    (directory / row.sha256).write_bytes(b"tampered")
    assert reconcile_uploads()["failed"] == 1
    row.refresh_from_db()
    assert row.state == "reserved" and not Blob.objects.exists()


@pytest.mark.parametrize("reason", ["busy", "corrupt"])
@pytest.mark.parametrize("retry_delay", [0, 60])
def test_old_failed_batch_rotates_past_thirty_two_entries(broker, settings, monkeypatch, reason, retry_delay):
    from compute_relay.control import storage
    clock = timezone.now()
    # Simulate abandoned historic rows without making physical upload attempts.
    blocked = [reserve(broker, ("old-" + str(index)).encode()) for index in range(33)]
    later = reserve(broker, b"later recoverable")
    UploadReservation.objects.filter(id__in=[row.id for row in blocked]).update(
        expires_at=clock - timedelta(seconds=60), created_at=clock - timedelta(minutes=2))
    UploadReservation.objects.filter(id=later.id).update(expires_at=clock - timedelta(seconds=30))
    original_finish = storage._finish
    def fail_old(row, **kwargs):
        if row.id != later.id:
            raise BrokerError("UPLOAD_BUSY" if reason == "busy" else "OBJECT_INTEGRITY", "synthetic")
        return original_finish(row, **kwargs)
    monkeypatch.setattr(storage, "_finish", fail_old)
    first = reconcile_uploads(limit=32, now=clock)
    assert first[reason if reason == "busy" else "failed"] == 32 and first["recovered"] == 0
    # Even if the caller waits past backoff (or restarts), retry timestamps order
    # prior failures behind older untouched eligible reservations.
    second = reconcile_uploads(limit=32, now=clock + timedelta(seconds=retry_delay))
    assert second["recovered"] == 1
    later.refresh_from_db()
    assert later.state == "aborted"
    assert UploadReservation.objects.filter(id__in=[row.id for row in blocked], state="reserved").count() == 33
    broker.project.refresh_from_db()
    assert broker.project.storage_reserved == sum(row.size_bytes for row in blocked)


def test_busy_backoff_does_not_overwrite_concurrently_finalized_state(broker, monkeypatch):
    from compute_relay.control import storage
    row = reserve(broker, b"alive")
    original_expiry = row.expires_at
    @contextmanager
    def finalized_by_writer(_):
        UploadReservation.objects.filter(id=row.id).update(state="finalized")
        raise BrokerError("UPLOAD_BUSY", "synthetic")
        yield
    monkeypatch.setattr(storage, "_writer_lock", finalized_by_writer)
    assert reconcile_uploads()["busy"] == 1
    row.refresh_from_db()
    assert row.state == "finalized" and row.expires_at == original_expiry


@pytest.mark.parametrize("stage,published", [
    ("file_fsync", False), ("publication", False), ("directory_fsync", True),
    ("finalization_once", True), ("finalization_unavailable", True),
])
def test_io_failures_before_and_after_publication_preserve_exact_charges(broker, settings, monkeypatch, stage, published):
    from compute_relay.control import storage
    data = b"synthetic publication failure test"
    original_finish = storage._finish
    if stage in {"file_fsync", "directory_fsync"}:
        original_fsync = os.fsync
        def fail_sync(fd):
            mode = os.fstat(fd).st_mode
            if (stage == "file_fsync" and stat.S_ISREG(mode)
                    or stage == "directory_fsync" and stat.S_ISDIR(mode)):
                raise OSError("synthetic fsync failure")
            return original_fsync(fd)
        monkeypatch.setattr(storage.os, "fsync", fail_sync)
    elif stage == "publication":
        monkeypatch.setattr(storage.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic link failure")))
    else:
        calls = []
        def fail_finish(*args, **kwargs):
            calls.append(True)
            if stage == "finalization_unavailable" or len(calls) == 1:
                raise OSError("synthetic database finalization failure")
            return original_finish(*args, **kwargs)
        monkeypatch.setattr(storage, "_finish", fail_finish)
    with pytest.raises(OSError):
        store_blob(broker.project, data)
    broker.project.refresh_from_db()
    pool = StoragePool.objects.get(id=1)
    row = UploadReservation.objects.get()
    destination = settings.ARTIFACT_ROOT / str(broker.project.id) / row.sha256
    assert destination.exists() is published
    assert not UploadGrant.objects.exists()
    if stage == "finalization_unavailable":
        assert row.state == "reserved" and not Blob.objects.exists()
        assert broker.project.storage_reserved == pool.reserved == len(data)
        monkeypatch.setattr(storage, "_finish", original_finish)
        UploadReservation.objects.filter(id=row.id).update(expires_at=timezone.now() - timedelta(seconds=1))
        assert reconcile_uploads()["recovered"] == 1
        broker.project.refresh_from_db()
        pool.refresh_from_db()
        row.refresh_from_db()
    assert broker.project.storage_reserved == pool.reserved == 0
    assert broker.project.storage_used == pool.used == (len(data) if published else 0)
    assert broker.project.object_count == (1 if published else 0)
    assert row.state == ("finalized" if published else "aborted")
    if published:
        assert read_blob(Blob.objects.get()) == data
