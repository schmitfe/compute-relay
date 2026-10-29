"""Burst traffic and outstanding work are independent of durable spending history."""

from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from django.test import Client
from django.utils import timezone

from compute_relay.control import admission, services
from compute_relay.control.errors import BrokerError
from compute_relay.control.metadata import metadata_usage
from compute_relay.control.models import (Attempt, ExecutionRequest, IdempotencyRecord, Job, LedgerEntry,
                                         Project, ProjectCredential)
from compute_relay.control.storage import store_uploaded_blob
from test_support.unit_test_control import broker, budgeted, decide, execution, prepared_job


@pytest.fixture
def clock(monkeypatch):
    current = [timezone.now()]
    monkeypatch.setattr(admission.timezone, "now", lambda: current[0])
    return current


def second_project(broker, request):
    project = Project.objects.create(name="another project", owner=broker.owner)
    project.connections.add(broker.connection)
    project.environments.add(broker.environment)
    credential = ProjectCredential.objects.create(project=project, issued_by=broker.owner, name="second", prefix="second",
        token_hash="1" * 64, scopes=broker.credential.scopes, expires_at=broker.credential.expires_at)
    blob = store_uploaded_blob(project, b"another input")
    document = deepcopy(request)
    document["project_id"] = str(project.id)
    document["files"][0].update(sha256=blob.sha256, size_bytes=blob.size_bytes)
    return project, credential, document


def test_default_burst_accepts_five_hundred_jobs_but_not_a_sustained_flood(broker, clock):
    job = prepared_job(broker)
    for _ in range(499):
        services.create_job(broker.credential, job.request)
    assert Job.objects.count() == 500
    assert metadata_usage(broker.project)["jobs"] == 500
    with pytest.raises(BrokerError) as error:
        services.create_job(broker.credential, job.request)
    assert (error.value.code, error.value.status) == ("RATE_LIMITED", 429)
    assert error.value.details == {"scope": "project", "operation": "create", "limit": 500,
                                  "per_minute": 60, "remaining": 0, "retry_after_seconds": 1}
    assert admission.admission_snapshot(broker.project)["outstanding"]["project"]["used"] == 500
    # Creating the burst has not consumed the independent execution-request bucket.
    execution(broker, job)
    assert admission.admission_snapshot(broker.project)["rates"]["execute"]["project"]["remaining"] == 499


def test_rate_refills_sustainably_and_clock_reversal_cannot_mint_tokens(broker, settings, clock):
    settings.PROJECT_ADMISSION_BURST = 2
    settings.PROJECT_ADMISSION_PER_MINUTE = 6
    job = prepared_job(broker)
    services.create_job(broker.credential, job.request)
    with pytest.raises(BrokerError) as error:
        services.create_job(broker.credential, job.request)
    assert error.value.details["retry_after_seconds"] == 10
    clock[0] += timedelta(seconds=10)
    services.create_job(broker.credential, job.request)
    clock[0] -= timedelta(seconds=60)
    with pytest.raises(BrokerError) as error:
        services.create_job(broker.credential, job.request)
    assert error.value.code == "RATE_LIMITED"
    assert error.value.details["retry_after_seconds"] == 70
    clock[0] += timedelta(seconds=70)
    services.create_job(broker.credential, job.request)
    assert Job.objects.count() == 4


def test_extra_keys_share_project_rate_and_projects_share_installation_rate(broker, settings, clock):
    settings.PROJECT_ADMISSION_BURST = 2
    settings.INSTALLATION_ADMISSION_BURST = 2
    job = prepared_job(broker)
    other_key = ProjectCredential.objects.create(project=broker.project, issued_by=broker.owner, name="another agent",
        prefix="another", token_hash="2" * 64, scopes=broker.credential.scopes, expires_at=broker.credential.expires_at)
    services.create_job(other_key, job.request)
    with pytest.raises(BrokerError) as error:
        services.create_job(broker.credential, job.request)
    assert error.value.details["scope"] == "project"
    other, credential, document = second_project(broker, job.request)
    with pytest.raises(BrokerError) as error:
        services.create_job(credential, document)
    assert error.value.details["scope"] == "installation"
    assert not Job.objects.filter(project=other).exists()
    other.refresh_from_db()
    assert other.admission_state == {}


def test_failed_admission_rolls_back_rate_state_drafts_and_receipts(broker, settings, clock):
    job = prepared_job(broker)
    before = admission.admission_snapshot(broker.project)
    settings.PROJECT_METADATA_LIMIT = metadata_usage(broker.project)["logical_bytes"] + 1000
    with pytest.raises(BrokerError) as error:
        services.idempotent(broker.project, "jobs:create", "too-large", job.request,
                           lambda: services.create_job(broker.credential, job.request))
    assert error.value.code == "METADATA_QUOTA"
    assert admission.admission_snapshot(broker.project) == before
    assert Job.objects.count() == 1 and not IdempotencyRecord.objects.exists()


def test_idempotent_retry_neither_consumes_another_token_nor_backlog_slot(broker, settings, clock):
    settings.PROJECT_ADMISSION_BURST = 2
    settings.MAX_PROJECT_OUTSTANDING_JOBS = 2
    job = prepared_job(broker)
    original = services.idempotent(broker.project, "jobs:create", "once", job.request,
                                  lambda: services.create_job(broker.credential, job.request))
    before = admission.admission_snapshot(broker.project)
    assert services.idempotent(broker.project, "jobs:create", "once", job.request,
                              lambda: pytest.fail("retry replayed admission")) == original
    assert admission.admission_snapshot(broker.project) == before
    assert Job.objects.count() == 2 and IdempotencyRecord.objects.count() == 1
    with pytest.raises(BrokerError) as error:
        services.idempotent(broker.project, "jobs:create", "new", job.request,
                            lambda: services.create_job(broker.credential, job.request))
    assert error.value.code == "BACKLOG_LIMIT"
    assert error.value.details["retry_after_seconds"] is None
    assert IdempotencyRecord.objects.count() == 1


def test_review_conversion_uses_one_slot_rejection_releases_it_and_rerun_needs_one(broker, settings, clock):
    settings.MAX_PROJECT_OUTSTANDING_JOBS = 1
    job = prepared_job(broker)
    review = execution(broker, job)
    assert admission.admission_snapshot(broker.project)["outstanding"]["project"]["used"] == 1
    with pytest.raises(BrokerError) as error:
        execution(broker, job)
    assert error.value.code == "BACKLOG_LIMIT"
    assert ExecutionRequest.objects.count() == 1
    decide(broker, job, review, approved=False)
    assert admission.admission_snapshot(broker.project)["outstanding"]["project"]["used"] == 0
    services.create_job(broker.credential, job.request)
    with pytest.raises(BrokerError) as error:
        execution(broker, job)
    assert error.value.code == "BACKLOG_LIMIT"
    assert Job.objects.count() == 2 and ExecutionRequest.objects.count() == 1
    assert not Attempt.objects.exists() and not LedgerEntry.objects.filter(action="reserve").exists()


def test_completion_keeps_history_and_holds_slot_until_collection_and_accounting_finish(broker, settings, clock):
    budgeted(broker)
    settings.MAX_PROJECT_OUTSTANDING_JOBS = 1
    job = prepared_job(broker)
    attempt = execution(broker, job).attempt
    Attempt.objects.filter(id=attempt.id).update(state="succeeded", collection_pending=True)
    assert admission.admission_snapshot(broker.project)["outstanding"]["project"]["used"] == 1
    Attempt.objects.filter(id=attempt.id).update(accounting_state="settled")
    assert admission.admission_snapshot(broker.project)["outstanding"]["project"]["used"] == 1
    Attempt.objects.filter(id=attempt.id).update(collection_pending=False)
    assert admission.admission_snapshot(broker.project)["outstanding"]["project"]["used"] == 0
    services.create_job(broker.credential, job.request)
    assert Job.objects.count() == 2 and Attempt.objects.count() == 1
    assert LedgerEntry.objects.filter(attempt=attempt, action="reserve").exists()
    assert services.allowance(broker.project)["jobs"]["admitted"] == 1
    assert metadata_usage(broker.project)["jobs"] == 2


def test_execution_overload_rolls_back_funds_and_cannot_be_bypassed_with_another_key(broker, settings, clock):
    budgeted(broker)
    settings.PROJECT_ADMISSION_BURST = 1
    job = prepared_job(broker)
    original = services.idempotent(broker.project, "execute", "first", {},
                                  lambda: services.request_execution(job, broker.credential))
    before = admission.admission_snapshot(broker.project)
    assert services.idempotent(broker.project, "execute", "first", {},
                              lambda: pytest.fail("retry repeated execution")) == original
    with pytest.raises(BrokerError) as error:
        services.idempotent(broker.project, "execute", "second", {},
                            lambda: services.request_execution(job, broker.credential))
    assert (error.value.code, error.value.status) == ("RATE_LIMITED", 429)
    assert admission.admission_snapshot(broker.project) == before
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 60
    assert Attempt.objects.count() == ExecutionRequest.objects.count() == IdempotencyRecord.objects.count() == 1
    assert LedgerEntry.objects.filter(action="reserve").count() == 1


def test_budget_rejection_rolls_back_the_execution_rate_charge(broker, clock):
    budgeted(broker, cpu=0)
    job = prepared_job(broker)
    before = admission.admission_snapshot(broker.project)
    with pytest.raises(BrokerError) as error:
        services.request_execution(job, broker.credential)
    assert error.value.code == "BUDGET_UNAVAILABLE"
    assert admission.admission_snapshot(broker.project) == before
    assert not ExecutionRequest.objects.exists() and not LedgerEntry.objects.exists()


def test_output_recovery_remains_available_at_capacity_without_admitting_more_compute(broker, settings, clock):
    budgeted(broker)
    settings.MAX_PROJECT_OUTSTANDING_JOBS = 1
    job = prepared_job(broker)
    attempt = execution(broker, job).attempt
    Attempt.objects.filter(id=attempt.id).update(state="succeeded", accounting_state="settled",
        collection_state="complete", native_identity={"job_id": "17", "attempt_id": str(attempt.id)},
        observation={"state": "completed", "terminal": True, "accounting": {"final": True}})
    services.create_job(broker.credential, job.request)
    assert services.retry_collection(attempt)[1] == 202
    assert admission.admission_snapshot(broker.project)["outstanding"]["project"]["used"] == 2
    with pytest.raises(BrokerError) as error:
        services.create_job(broker.credential, job.request)
    assert error.value.code == "BACKLOG_LIMIT"
    assert Attempt.objects.count() == 1 and Job.objects.count() == 2


def test_http_error_and_allowance_explain_rate_capacity_and_retry(broker, settings, clock):
    settings.PROJECT_ADMISSION_BURST = 1
    job = prepared_job(broker)
    client = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    response = client.post("/api/v1/jobs", data=json.dumps(job.request), content_type="application/json",
                           HTTP_IDEMPOTENCY_KEY="overload")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.json()["error"]["details"]["retry_after_seconds"] == 1
    assert response["Retry-After"] == "1"
    assert services.allowance(broker.project)["admission"]["rates"]["create"]["project"]["remaining"] == 0
    assert not IdempotencyRecord.objects.exists()


@pytest.mark.parametrize("bound", ["rate", "backlog"])
@pytest.mark.parametrize("writers", ["threads", "processes"])
def test_installation_bounds_serialize_simultaneous_project_writers(tmp_path, bound, writers):
    script = r'''
import django
django.setup()
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import multiprocessing
from django.conf import settings
from django.core.management import call_command
from django.db import close_old_connections, connections, transaction
from compute_relay.control.admission import admit, admission_snapshot
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Job, MetadataPool, Project, User
call_command("migrate", verbosity=0)
owner=User.objects.create_user(username="admission-race")
projects=[Project.objects.create(owner=owner,name=str(i)) for i in range(4)]
import os
bound=os.environ["ADMISSION_TEST_BOUND"]
settings.INSTALLATION_ADMISSION_PER_MINUTE=1
settings.INSTALLATION_ADMISSION_BURST=1 if bound=="rate" else 100
settings.MAX_INSTALLATION_OUTSTANDING_JOBS=1 if bound=="backlog" else 100
context=multiprocessing.get_context("fork")
barrier=context.Barrier(4)
def write(index):
    if os.environ["ADMISSION_TEST_WRITERS"]=="processes":
        connections.close_all()
    close_old_connections()
    barrier.wait(timeout=10)
    try:
        with transaction.atomic():
            admit(projects[index], "create")
            Job.objects.create(project=projects[index], request={})
        return "stored"
    except BrokerError as error:
        return error.code
    finally:
        close_old_connections()
executor=(ProcessPoolExecutor(max_workers=4, mp_context=context) if os.environ["ADMISSION_TEST_WRITERS"]=="processes"
    else ThreadPoolExecutor(max_workers=4))
with executor:
    outcomes=list(executor.map(write,range(4)))
expected="RATE_LIMITED" if bound=="rate" else "BACKLOG_LIMIT"
assert outcomes.count("stored")==1 and outcomes.count(expected)==3,outcomes
assert Job.objects.count()==1
assert sum(bool(project.admission_state) for project in Project.objects.all())==1
print("bounded")
'''
    environment = dict(os.environ, DJANGO_SETTINGS_MODULE="compute_relay.control.settings", BROKER_EXTENSIONS="",
        PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"), BROKER_DATA_DIR=str(tmp_path / "state"),
        BROKER_DB_PATH=str(tmp_path / "race.sqlite3"), ADMISSION_TEST_BOUND=bound, ADMISSION_TEST_WRITERS=writers)
    process = subprocess.run([sys.executable, "-c", script], env=environment, text=True, capture_output=True, timeout=30)
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "bounded"
