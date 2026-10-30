"""Status pages stay small and release the writer while retaining global truth."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
import pytest

from compute_relay.control import completion, services
from compute_relay.control.database import read_snapshot
from compute_relay.control.models import Artifact, Attempt, ExecutionRequest, ResultAccess
from compute_relay.control.storage import store_blob
from test_support.unit_test_control import broker, budgeted, dispatched, prepared_job, submit_receipt


def history(context, job, count=45):
    requests = [ExecutionRequest(job=job, credential=context.credential, state="approved") for _ in range(count)]
    ExecutionRequest.objects.bulk_create(requests)
    attempts = [Attempt(job=job, execution_request=item, connection=context.connection, grant=context.grant,
        state="succeeded", accounting_state="released", reservation_cpu=60, reservation_gpu=0,
        collection_state="complete", observation={"private": "x" * 65536}, stdout="x" * 65536) for item in requests]
    Attempt.objects.bulk_create(attempts)
    return attempts


def test_status_pages_preserve_global_counts_and_do_not_format_off_page_history(broker, monkeypatch):
    job = prepared_job(broker)
    rows = history(broker, job)
    Attempt.objects.filter(id=rows[-1].id).update(state="submission_uncertain", accounting_state="reserved", dispatch_started=True)
    allowed = {rows[2].id, rows[3].id}
    original = completion.serialize_attempt
    def serialize(attempt):
        assert attempt.id in allowed, "off-page diagnostics must not be loaded/formatted"
        return original(attempt)
    monkeypatch.setattr(completion, "serialize_attempt", serialize)
    client = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    with CaptureQueriesContext(connection) as queries:
        result = client.get(f"/api/v1/jobs/{job.id}/completion", {"cursor": 2, "execution_cursor": 2, "limit": 2})
    assert result.status_code == 200
    value = result.json()
    assert {item["id"] for item in value["attempts"]} == {str(identifier) for identifier in allowed}
    assert len(value["execution_requests"]) == 2
    assert value["next_cursor"] == value["next_execution_cursor"] == 4
    assert value["summary"]["attempts"] == 45 and value["summary"]["uncertain_submissions"] == 1
    assert value["summary"]["unacknowledged_attempts"] == 44
    assert value["summary"]["reserved"]["cpu_core_seconds"] == 60
    assert not value["summary"]["accounting_final"] and not value["summary"]["operationally_reconciled"]
    assert any(item["attempt_id"] == str(rows[-1].id) for item in value["summary"]["blockers"])
    full_attempt_reads = [item["sql"] for item in queries if f'FROM "{Attempt._meta.db_table}" ' in item["sql"] and f'"{Attempt._meta.db_table}"."stdout"' in item["sql"]]
    assert len(full_attempt_reads) == 1 and "LIMIT 2 OFFSET 2" in full_attempt_reads[0]
    assert len(queries) < 40
    monkeypatch.setattr(completion, "serialize_attempt", original)
    newest = client.get(f"/api/v1/jobs/{job.id}", {"limit": 2}).json()
    assert newest["attempts"][-1]["id"] == str(rows[-1].id)
    assert newest["attempt_cursor"] == 43 and newest["previous_attempt_cursor"] == 41
    assert newest["execution_progress"]["submission_uncertain"]


def test_off_page_reports_artifacts_and_delivery_invalidate_every_page(broker):
    budgeted(broker)
    job = prepared_job(broker, outputs=["answer.txt"])
    history(broker, job, count=22)
    attempt, lease = dispatched(broker, job)
    native, lease = submit_receipt(broker, attempt, lease)
    client = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    path = f"/api/v1/jobs/{job.id}/completion"
    def page(cursor=0):
        response = client.get(path, {"cursor": cursor, "limit": 1})
        assert response.status_code == 200
        return response.json()
    first = page()
    assert first["snapshot_revision"] == page(22)["snapshot_revision"]
    detail = client.get(f"/api/v1/jobs/{job.id}", {"cursor": 0, "limit": 1}).json()
    services.report(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "outcome": "observed", "native_identity": native, "stdout": "new progress", "observation": {
            "state": "running", "terminal": False, "accounting": {"final": False}}})
    reported = page()
    assert first["attempts"] == reported["attempts"]
    assert first["snapshot_revision"] != reported["snapshot_revision"]
    changed = client.get(f"/api/v1/jobs/{job.id}", {"cursor": 0, "limit": 1, "after_revision": detail["revision"]}).json()
    assert changed["changed"]
    Artifact.objects.create(attempt=attempt, path="answer.txt", blob=store_blob(broker.project, b"answer"))
    artifact = page()
    assert artifact["snapshot_revision"] != reported["snapshot_revision"]
    ResultAccess.objects.create(credential=broker.credential, attempt=attempt)
    assert page()["snapshot_revision"] != artifact["snapshot_revision"]


def test_blocker_list_is_bounded_without_claiming_completion(broker):
    job = prepared_job(broker)
    history(broker, job)
    Attempt.objects.filter(job=job).update(state="running", accounting_state="reserved")
    response = Client(HTTP_AUTHORIZATION="Bearer " + broker.token).get(f"/api/v1/jobs/{job.id}/completion").json()
    summary = response["summary"]
    assert summary["blocker_count"] == 90 and len(summary["blockers"]) == 20
    assert summary["blockers_truncated"] and not summary["operationally_reconciled"]
    assert summary["reserved"]["cpu_core_seconds"] == 45 * 60


@pytest.mark.django_db(transaction=True)
def test_read_snapshot_keeps_normal_immediate_writes_and_does_not_reserve_writer(broker):
    assert not connection.in_atomic_block
    mode = connection.transaction_mode
    with CaptureQueriesContext(connection) as queries:
        with read_snapshot():
            assert connection.in_atomic_block
            assert broker.project.__class__.objects.filter(id=broker.project.id).exists()
            # Another connection can still reserve SQLite's single writer slot.
            other = sqlite3.connect(connection.settings_dict["NAME"], uri=True, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
            finally:
                other.close()
        with transaction.atomic():
            pass
    assert connection.transaction_mode == mode == "IMMEDIATE"
    begins = [item["sql"] for item in queries if item["sql"].startswith("BEGIN")]
    assert begins == ["BEGIN DEFERRED", "BEGIN IMMEDIATE"]
    with pytest.raises(RuntimeError):
        with read_snapshot():
            raise RuntimeError("rollback")
    assert connection.transaction_mode == "IMMEDIATE" and not connection.in_atomic_block


@pytest.mark.parametrize("query", [{"cursor": -1}, {"execution_cursor": -1}, {"cursor": 2**64},
    {"execution_cursor": 2**64}, {"limit": 21}])
def test_status_rejects_out_of_bounds_database_pagination(broker, query):
    job = prepared_job(broker)
    client = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    for suffix in ("", "/completion"):
        assert client.get(f"/api/v1/jobs/{job.id}{suffix}", query).status_code == 400


@pytest.mark.django_db(transaction=True)
def test_read_snapshot_initializes_a_fresh_thread_connection():
    def fresh_request():
        database = transaction.get_connection()
        assert database.connection is None
        assert not hasattr(database, "transaction_mode")
        statements = []
        def record(execute, sql, params, many, context):
            statements.append(sql)
            return execute(sql, params, many, context)
        try:
            # CaptureQueriesContext would open the connection before our helper,
            # hiding the lazy initialization exercised by a new HTTP thread.
            with database.execute_wrapper(record):
                with read_snapshot():
                    with database.cursor() as cursor:
                        cursor.execute("SELECT 1")
                        assert cursor.fetchone() == (1,)
                with transaction.atomic():
                    with database.cursor() as cursor:
                        cursor.execute("CREATE TEMP TABLE snapshot_write(value INTEGER)")
                        cursor.execute("INSERT INTO snapshot_write VALUES (1)")
            assert database.transaction_mode == "IMMEDIATE"
            assert not database.in_atomic_block
            return [sql for sql in statements if sql.startswith("BEGIN")]
        finally:
            database.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(fresh_request).result(timeout=10) == ["BEGIN DEFERRED", "BEGIN IMMEDIATE"]
