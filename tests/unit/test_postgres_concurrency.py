"""Exercise independent SQL clients against actual PostgreSQL row locks."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.conf import settings
from django.db import close_old_connections, connection, transaction

from compute_broker.control import services
from compute_broker.control.errors import BrokerError
from compute_broker.control.models import Attempt, LedgerEntry, Project, ProjectCredential, User, WorkerIdentity
from test_control import broker, budgeted, prepared_job


CHILD = r'''
import json,os,time,django
from pathlib import Path
django.setup()
from django.db import transaction
from compute_broker.control import services
from compute_broker.control.models import Job,ProjectCredential,WorkerIdentity,Attempt
from compute_broker.control.errors import BrokerError
gate=Path(os.environ['BROKER_RACE_GATE'])
deadline=time.monotonic()+20
while not gate.exists():
    if time.monotonic()>deadline:raise RuntimeError('race gate timeout')
    time.sleep(.01)
mode=os.environ['BROKER_RACE_MODE']
try:
    if mode=='admit':
        with transaction.atomic():
            services.request_execution(Job.objects.get(id=os.environ['BROKER_RACE_JOB']),ProjectCredential.objects.get(id=os.environ['BROKER_RACE_KEY']))
        result='authorized'
    elif mode=='lease':
        lease=services.lease_work(WorkerIdentity.objects.get(id=os.environ['BROKER_RACE_WORKER']))['lease']
        result='leased' if lease else 'empty'
    else:
        services.cancel_attempt(Attempt.objects.get(id=os.environ['BROKER_RACE_ATTEMPT']))
        result='cancelled'
except BrokerError as error:
    result=error.code
print(json.dumps({'result':result}))
'''


def race(context, tmp_path, mode, *, job, attempt=None, count=6):
    if connection.vendor != "postgresql":
        pytest.skip("Requires the PostgreSQL integration job")
    gate = tmp_path / "start"
    secret = tmp_path / "django-secret"
    secret.write_text("synthetic-process-test-secret-with-no-production-authority")
    secret.chmod(0o600)
    environment = dict(os.environ, DJANGO_SETTINGS_MODULE="compute_broker.control.settings",
        BROKER_DB_ENGINE="postgresql", BROKER_DB_NAME=connection.settings_dict["NAME"],
        BROKER_EXTENSIONS=",".join(settings.BROKER_EXTENSIONS),
        BROKER_DATA_DIR=str(tmp_path / "child-service"), BROKER_SECRET_KEY_FILE=str(secret),
        BROKER_ARTIFACT_ROOT=str(settings.ARTIFACT_ROOT),
        BROKER_RACE_GATE=str(gate), BROKER_RACE_MODE=mode, BROKER_RACE_JOB=str(job.id),
        BROKER_RACE_KEY=str(context.credential.id), BROKER_RACE_WORKER=str(context.worker.id),
        BROKER_RACE_ATTEMPT=str(attempt.id) if attempt else "")
    processes = [subprocess.Popen([sys.executable, "-c", CHILD], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(count)]
    try:
        gate.touch()
        results = []
        for process in processes:
            out, err = process.communicate(timeout=35)
            assert process.returncode == 0, err
            results.append(json.loads(out)["result"])
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()


@pytest.mark.django_db(transaction=True)
def test_postgres_processes_share_one_admission_balance(broker, tmp_path):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL")
    budgeted(broker)
    broker.grant.refresh_from_db()
    broker.grant.policy["allowance"]["cpu_core_seconds"] = 180
    broker.grant.save(update_fields=["policy"])
    job = prepared_job(broker)
    outcomes = race(broker, tmp_path, "admit", job=job)
    assert outcomes.count("authorized") == 3 and outcomes.count("BUDGET_UNAVAILABLE") == 3
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 180 and Attempt.objects.count() == 3
    assert LedgerEntry.objects.filter(action="reserve").count() == 3


@pytest.mark.django_db(transaction=True)
def test_postgres_processes_cannot_claim_one_attempt_twice(broker, tmp_path):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL")
    budgeted(broker)
    job = prepared_job(broker)
    with transaction.atomic():
        services.request_execution(job, broker.credential)
    outcomes = race(broker, tmp_path, "lease", job=job)
    assert outcomes.count("leased") == 1 and outcomes.count("empty") == 5
    assert Attempt.objects.get().generation == 1


@pytest.mark.django_db(transaction=True)
def test_postgres_processes_release_cancelled_reservation_once(broker, tmp_path):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL")
    budgeted(broker)
    job = prepared_job(broker)
    with transaction.atomic():
        services.request_execution(job, broker.credential)
    outcomes = race(broker, tmp_path, "cancel", job=job, attempt=Attempt.objects.get())
    assert outcomes == ["cancelled"] * 6
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 0
    assert LedgerEntry.objects.filter(action="release").count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["pause", "revoke", "review_required", "disable_user"])
def test_postgres_dispatch_waits_for_current_authority(broker, change):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL")
    budgeted(broker)
    job = prepared_job(broker)
    with transaction.atomic():
        services.request_execution(job, broker.credential)
    lease = services.lease_work(broker.worker)["lease"]
    attempt = Attempt.objects.get()
    document = {"lease_id": lease["id"], "generation": lease["generation"],
        "manifest_sha256": job.manifest_sha256, "connection_revision_sha256": broker.connection.revision_sha256}
    blocked = Event()
    table = '"broker_user"' if change == "disable_user" else '"broker_project"'

    def dispatch():
        close_old_connections()
        def trace(execute, sql, params, many, context):
            if "FOR " in sql and ("FROM " + table) in sql:
                blocked.set()
            return execute(sql, params, many, context)
        try:
            with connection.execute_wrapper(trace):
                services.dispatch(WorkerIdentity.objects.get(id=broker.worker.id), attempt.id, document)
            return "authorized"
        except BrokerError as exc:
            return exc.code
        finally:
            close_old_connections()

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with transaction.atomic():
            if change == "disable_user":
                User.objects.select_for_update(no_key=True).get(id=broker.owner.id)
            else:
                Project.objects.select_for_update().get(id=broker.project.id)
            result = pool.submit(dispatch)
            assert blocked.wait(timeout=5), "Dispatch failed to acquire its authority lock"
            assert not result.done(), "Dispatch bypassed an uncommitted authority change"
            if change == "pause":
                Project.objects.filter(id=broker.project.id).update(paused=True)
            elif change == "revoke":
                ProjectCredential.objects.filter(id=broker.credential.id).update(revoked=True)
            elif change == "review_required":
                ProjectCredential.objects.filter(id=broker.credential.id).update(execution_mode="approval_required")
            else:
                User.objects.filter(id=broker.owner.id).update(is_active=False)
        assert result.result(timeout=10) in {"DISPATCH_REVOKED", "AUTH_REQUIRED"}
        attempt.refresh_from_db()
        assert not attempt.dispatch_started and attempt.state == "authorized"
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
