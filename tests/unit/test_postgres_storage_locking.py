"""Storage reservations must compose with the core's outer project lock.

Independent PostgreSQL connections prove this boundary; SQLite's database-wide
write serialization cannot exercise the reverse row-lock deadlock.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident

import pytest
from django.db import close_old_connections, connection, transaction

from compute_broker.control import services
from compute_broker.control import storage
from compute_broker.control.models import Blob, Project, StoragePool, UploadReservation
from compute_broker.control.storage import read_blob, store_blob
from test_control import broker


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("phase", ["reservation", "publication", "abort_cleanup"])
def test_postgres_blob_write_does_not_reverse_outer_idempotency_project_lock(broker, monkeypatch, phase):
    if connection.vendor != "postgresql":
        pytest.skip("Requires actual PostgreSQL row locks")

    pool, _ = StoragePool.objects.get_or_create(id=1)
    project = broker.project
    baseline_used = pool.used
    baseline_project_used = project.storage_used
    baseline_count = project.object_count
    main_bytes = b"synthetic main upload under an idempotency project lock\n"
    concurrent_bytes = b"synthetic independent upload to the same project\n"
    project_lock_requested = Event()
    publication_ready = Event()
    allow_publication = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    pending = None
    concurrent_thread_id = None

    class SimulatedWriteFailure(OSError):
        pass

    original_link = storage.os.link

    def controlled_publication(*args, **kwargs):
        if get_ident() == concurrent_thread_id and phase != "reservation":
            # The reservation committed before this external file operation.
            # Let the main transaction take its project lock before the writer
            # reaches the publication or error-cleanup metadata transaction.
            publication_ready.set()
            assert allow_publication.wait(timeout=5), "Main transaction did not release the file-operation gate"
            if phase == "abort_cleanup":
                raise SimulatedWriteFailure("Synthetic publication failure")
        return original_link(*args, **kwargs)

    monkeypatch.setattr(storage.os, "link", controlled_publication)

    def concurrent_upload():
        nonlocal concurrent_thread_id
        concurrent_thread_id = get_ident()
        close_old_connections()
        project_requests = 0

        def trace(execute, sql, params, many, context):
            nonlocal project_requests
            if 'FROM "broker_project"' in sql and "FOR UPDATE" in sql:
                project_requests += 1
                # On the old Pool -> Project order this signal occurs while
                # the competing transaction already owns the pool row. On the
                # corrected order it owns no pool row and waits for our project.
                if project_requests == (1 if phase == "reservation" else 2):
                    project_lock_requested.set()
            return execute(sql, params, many, context)

        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '10s'")
            with connection.execute_wrapper(trace):
                current = Project.objects.get(id=project.id)
                try:
                    return store_blob(current, concurrent_bytes).id
                except SimulatedWriteFailure:
                    assert phase == "abort_cleanup"
                    return None
        finally:
            # Release all locks even when running against a broken lock order.
            connection.close()

    def idempotent_upload():
        nonlocal pending
        if phase == "reservation":
            pending = executor.submit(concurrent_upload)
        else:
            allow_publication.set()
        assert project_lock_requested.wait(timeout=5), "Concurrent upload did not reach the project row lock"
        # The competing connection is blocked on the project held by
        # services.idempotent. It must not hold a pool lock needed by this call.
        blob = store_blob(project, main_bytes)
        assert not pending.done(), "Concurrent upload escaped or failed while the outer project lock was held"
        return {"blob_id": str(blob.id)}, 201

    try:
        if phase != "reservation":
            pending = executor.submit(concurrent_upload)
            assert publication_ready.wait(timeout=5), "Concurrent writer did not commit its reservation before publication"
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Bound failure on a regression even if server deadlock
                # detection is configured with a longer timeout.
                cursor.execute("SET LOCAL lock_timeout = '3s'")
            result, status = services.idempotent(project, "test:storage-lock-order", "main-upload",
                {"synthetic": "main-upload"}, idempotent_upload)
            assert status == 201
        other_id = pending.result(timeout=12)
    finally:
        # The atomic block has released the project before joining the writer.
        allow_publication.set()
        executor.shutdown(wait=True, cancel_futures=True)

    main_blob = Blob.objects.get(id=result["blob_id"])
    assert read_blob(main_blob) == main_bytes
    if phase == "abort_cleanup":
        assert other_id is None
    else:
        other_blob = Blob.objects.get(id=other_id)
        assert main_blob.id != other_blob.id and read_blob(other_blob) == concurrent_bytes
    project.refresh_from_db()
    pool.refresh_from_db()
    size = len(main_bytes) + (0 if phase == "abort_cleanup" else len(concurrent_bytes))
    assert project.storage_used == baseline_project_used + size
    assert project.storage_reserved == 0 and project.object_count == baseline_count + (1 if phase == "abort_cleanup" else 2)
    assert pool.used == baseline_used + size and pool.reserved == 0
    states = sorted(UploadReservation.objects.filter(project=project).values_list("state", flat=True))
    assert states == (["aborted", "finalized"] if phase == "abort_cleanup" else ["finalized", "finalized"])
