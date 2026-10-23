"""Lifetime submission limits use the same durable admission boundary as compute."""

import copy
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.db import transaction
from django.utils import timezone
from jsonschema import ValidationError

from compute_broker.control import services
from compute_broker.control.auth import token_hash
from compute_broker.control.errors import BrokerError
from compute_broker.control.models import (
    ApprovalDecision, Attempt, Connection, Environment, ExecutionRequest, Grant, Job,
    LedgerEntry, Project, ProjectCredential, User, WorkerIdentity,
)
from compute_broker.domain.validation import validate


@pytest.fixture
def capped_project(db):
    owner = User.objects.create_user(username="submission-limit-owner", password=None)
    worker = WorkerIdentity.objects.create(owner=owner, name="submission-limit-worker", token_hash=token_hash("limit-worker"))
    connection = Connection.objects.create(owner=owner, worker=worker, name="simulated", backend="fake",
                                           configuration={}, revision_sha256="c" * 64)
    project = Project.objects.create(name="capped-project", owner=owner)
    project.connections.add(connection)
    environment = Environment.objects.create(owner=owner, connection=connection, name="cluster Python",
        configuration={"interpreter": "/usr/bin/python3"}, revision_sha256="e" * 64)
    project.environments.add(environment)
    credential = ProjectCredential.objects.create(
        project=project, issued_by=project.owner, name="first key", prefix="synthetic", token_hash=token_hash("limit-key"),
        scopes=["jobs:write", "jobs:execute", "jobs:read", "jobs:cancel"],
        expires_at=timezone.now() + timedelta(days=1))
    policy = {"schema_version": "1.0", "project_id": str(project.id), "revision": 1,
              "mode": "budgeted", "allowed_connection_ids": [str(connection.id)],
              "allowed_environment_revision_sha256": ["e" * 64], "allowed_gpu_types": [],
              "per_job_limits": {"cpu_cores": 1, "gpu_devices": 0, "memory_mib": 128,
                                 "time_limit_seconds": 60, "maximum_charge_duration_seconds": 60},
              "max_active_jobs": 100, "max_total_jobs": 1,
              "allowance": {"cpu_core_seconds": 100000, "gpu_device_seconds": 0},
              "admission_expires_at": (timezone.now() + timedelta(days=1)).isoformat()}
    grant = Grant.objects.create(project=project, policy=policy)
    # A prepared package is sufficient for these admission tests. No worker or
    # scheduler executes this synthetic package and it contains no source files.
    job = Job.objects.create(project=project, credential=credential, state="prepared",
        request={"connection_id": str(connection.id), "name": "limit-test", "outputs": [],
                 "environment": {"id": str(environment.id), "revision_sha256": environment.revision_sha256}, "resources": {"memory_mib": 128}},
        manifest={"connection_revision_sha256": "c" * 64,
                  "maximum_allocation": {"cpu_cores": 1, "gpu_devices": 0, "time_limit_seconds": 60,
                                         "maximum_charge_duration_seconds": 60},
                  "reservation": {"cpu_core_seconds": 60, "gpu_device_seconds": 0}},
        manifest_sha256="f" * 64)
    return SimpleNamespace(owner=owner, worker=worker, connection=connection, project=project,
                           credential=credential, grant=grant, job=job)


def configure(context, **changes):
    context.grant.policy = {**context.grant.policy, **changes}
    context.grant.save(update_fields=["policy"])


def execute(context, *, credential=None, job=None):
    with transaction.atomic():
        result, _ = services.request_execution(job or context.job, credential or context.credential)
    return ExecutionRequest.objects.get(id=result["id"])


def decide(context, execution, approved=True):
    with transaction.atomic():
        return services.decide(execution.job, context.owner, {
            "execution_request_id": str(execution.id), "manifest_sha256": execution.job.manifest_sha256,
            "approved": approved, "comment": "Synthetic lifetime-limit test"})


def assert_cap(action):
    with pytest.raises(BrokerError) as error:
        action()
    assert (error.value.code, error.value.status) == ("TOTAL_JOB_LIMIT", 409)


def test_last_slot_is_admitted_once_and_reported_in_allowance(capped_project):
    context = capped_project
    assert services.allowance(context.project)["jobs"] == {
        "limit": 1, "admitted": 0, "remaining": 1, "active": 0, "active_limit": 100, "awaiting_approval": 0}
    execute(context)
    assert_cap(lambda: execute(context))
    assert Attempt.objects.count() == 1
    assert ExecutionRequest.objects.count() == 1  # Denied admission rolled back.
    assert services.allowance(context.project)["jobs"] == {
        "limit": 1, "admitted": 1, "remaining": 0, "active": 1, "active_limit": 100, "awaiting_approval": 0}
    assert LedgerEntry.objects.filter(action="reserve").count() == 1


def test_legacy_policy_has_a_finite_default_and_zero_explicitly_blocks_admission(capped_project):
    context = capped_project
    del context.grant.policy["max_total_jobs"]
    context.grant.save(update_fields=["policy"])
    assert services.allowance(context.project)["jobs"]["limit"] == services.DEFAULT_MAX_TOTAL_JOBS == 1000
    configure(context, max_total_jobs=0)
    assert_cap(lambda: execute(context))
    assert Attempt.objects.count() == 0
    assert services.allowance(context.project)["jobs"]["remaining"] == 0


@pytest.mark.parametrize("value", [-1, 1000001, True, 1.5, "10", None])
def test_policy_schema_rejects_invalid_total_job_limits(capped_project, value):
    policy = {**capped_project.grant.policy, "max_total_jobs": value}
    with pytest.raises(ValidationError):
        validate("project-policy.schema.json", policy)


@pytest.mark.parametrize("value", [0, 1, 1000000])
def test_policy_schema_accepts_bounded_total_job_limits(capped_project, value):
    validate("project-policy.schema.json", {**capped_project.grant.policy, "max_total_jobs": value})


def test_waiting_and_denied_reviews_do_not_consume_slots_and_approval_cannot_override_cap(capped_project):
    context = capped_project
    configure(context, mode="approval_required")
    denied = execute(context)
    first = execute(context)
    waiting = execute(context)
    decide(context, denied, approved=False)
    assert services.allowance(context.project)["jobs"]["admitted"] == 0
    decide(context, first)
    assert_cap(lambda: decide(context, waiting))
    waiting.refresh_from_db()
    denied.refresh_from_db();first.refresh_from_db();context.grant.refresh_from_db()
    assert waiting.state == "awaiting_approval" and waiting.reservation_state == "held"
    assert denied.reservation_state == "released" and first.reservation_state == "converted"
    assert context.grant.reserved_cpu == 120
    assert services.allowance(context.project)["jobs"]["awaiting_approval"] == 1
    assert not ApprovalDecision.objects.filter(execution_request=waiting).exists()
    assert ApprovalDecision.objects.filter(approved=True, consumed_at__isnull=False).count() == 1
    assert Attempt.objects.count() == 1


def test_cancelled_attempt_refunds_compute_but_never_refunds_submission_slot(capped_project):
    context = capped_project
    attempt = execute(context).attempt
    services.cancel_attempt(attempt)
    context.grant.refresh_from_db()
    assert context.grant.reserved_cpu == 0
    jobs = services.allowance(context.project)["jobs"]
    assert jobs == {"limit": 1, "admitted": 1, "remaining": 0, "active": 0, "active_limit": 100, "awaiting_approval": 0}
    assert_cap(lambda: execute(context))


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "expired"])
def test_settled_terminal_attempt_does_not_reset_lifetime_limit(capped_project, terminal):
    context = capped_project
    attempt = execute(context).attempt
    with transaction.atomic():
        services._settle(attempt, 1, 0, {"source": "synthetic_accounting"})
        attempt.state = terminal
        attempt.save()
    jobs = services.allowance(context.project)["jobs"]
    assert (jobs["admitted"], jobs["remaining"], jobs["active"]) == (1, 0, 0)
    assert_cap(lambda: execute(context))
    assert LedgerEntry.objects.filter(action="settle").count() == 1


def test_multiple_and_rotated_keys_share_the_same_lifetime_limit(capped_project):
    context = capped_project
    configure(context, max_total_jobs=2)
    second = ProjectCredential.objects.create(
        project=context.project, issued_by=context.project.owner, name="second key", prefix="synthetic2", token_hash=token_hash("limit-second"),
        scopes=context.credential.scopes, expires_at=timezone.now() + timedelta(days=1))
    execute(context)
    execute(context, credential=second)
    replacement = ProjectCredential.objects.create(
        project=context.project, issued_by=context.project.owner, name="replacement key", prefix="synthetic3", token_hash=token_hash("limit-replacement"),
        scopes=context.credential.scopes, expires_at=timezone.now() + timedelta(days=1))
    context.credential.revoked = True
    context.credential.replaced_by = replacement
    context.credential.save(update_fields=["revoked", "replaced_by"])
    assert_cap(lambda: execute(context, credential=replacement))
    assert Attempt.objects.count() == 2
    assert services.allowance(context.project)["jobs"]["remaining"] == 0


def test_prepared_revision_and_idempotent_replay_do_not_create_extra_attempts(capped_project):
    context = capped_project
    key = "one-admission"
    payload = {"job_id": str(context.job.id)}
    first = services.idempotent(context.project, "execute", key, payload,
        lambda: services.request_execution(context.job, context.credential))
    replay = services.idempotent(context.project, "execute", key, payload,
        lambda: pytest.fail("idempotent replay tried new authorization"))
    assert replay == first and Attempt.objects.count() == 1
    revision = Job.objects.create(project=context.project, credential=context.credential, previous=context.job,
        state="prepared", request=copy.deepcopy(context.job.request), manifest=copy.deepcopy(context.job.manifest),
        manifest_sha256="d" * 64)
    assert services.prepare_job(revision)[1] == 200
    assert Attempt.objects.count() == 1
    assert_cap(lambda: execute(context, job=revision))


def test_reconciliation_lease_preserves_the_existing_admission_slot(capped_project):
    context = capped_project
    attempt = execute(context).attempt
    lease = services.lease_work(context.worker)["lease"]
    services.dispatch(context.worker, attempt.id, {
        "lease_id": lease["id"], "generation": lease["generation"],
        "manifest_sha256": context.job.manifest_sha256,
        "connection_revision_sha256": context.connection.revision_sha256})
    report = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "submission_uncertain"}
    assert services.report(context.worker, attempt.id, report)["accepted"]
    assert services.report(context.worker, attempt.id, report)["accepted"]  # receipt replay
    Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now() - timedelta(seconds=1))
    assert services.lease_work(context.worker)["lease"]["operation"] == "reconcile"
    assert Attempt.objects.count() == 1
    assert services.allowance(context.project)["jobs"]["admitted"] == 1


@pytest.mark.parametrize("mode", ["budgeted", "approval_required"])
def test_file_backed_admission_race_cannot_overbook_the_last_lifetime_slot(tmp_path, mode):
    """Separate connections contend for a durable SQLite DB and one final slot."""
    script = r'''
import json, os, django
django.setup()
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from django.core.management import call_command
from django.db import close_old_connections, transaction
from django.utils import timezone
from compute_broker.control.models import *
from compute_broker.control.auth import token_hash
from compute_broker.control import services
from compute_broker.control.errors import BrokerError
call_command("migrate", run_syncdb=True, verbosity=0)
owner=User.objects.create_user(username="limit-race-owner", password=None)
worker=WorkerIdentity.objects.create(owner=owner,name="fake",token_hash=token_hash("limit-race-worker"))
connection=Connection.objects.create(owner=owner,worker=worker,name="fake",backend="fake",configuration={},revision_sha256="c"*64)
project=Project.objects.create(owner=owner,name="lifetime-race")
project.connections.add(connection)
environment=Environment.objects.create(owner=owner,connection=connection,name="cluster Python",configuration={"interpreter":"/usr/bin/python3"},revision_sha256="e"*64)
project.environments.add(environment)
credential=ProjectCredential.objects.create(project=project, issued_by=project.owner,name="agent",prefix="fake",token_hash=token_hash("limit-race-key"),scopes=["jobs:execute"],expires_at=timezone.now()+timedelta(days=1))
mode=os.environ["SUBMISSION_LIMIT_TEST_MODE"]
policy={"schema_version":"1.0","project_id":str(project.id),"revision":1,"mode":mode,"allowed_connection_ids":[str(connection.id)],"allowed_environment_revision_sha256":["e"*64],"allowed_gpu_types":[],"per_job_limits":{"cpu_cores":1,"gpu_devices":0,"memory_mib":128,"time_limit_seconds":60,"maximum_charge_duration_seconds":60},"max_active_jobs":100,"max_total_jobs":2,"allowance":{"cpu_core_seconds":100000,"gpu_device_seconds":0},"admission_expires_at":(timezone.now()+timedelta(days=1)).isoformat()}
grant=Grant.objects.create(project=project,policy=policy)
job=Job.objects.create(project=project,credential=credential,request={"connection_id":str(connection.id),"environment":{"id":str(environment.id),"revision_sha256":environment.revision_sha256},"resources":{"memory_mib":128}},manifest={"connection_revision_sha256":"c"*64,"maximum_allocation":{"cpu_cores":1,"gpu_devices":0,"time_limit_seconds":60,"maximum_charge_duration_seconds":60},"reservation":{"cpu_core_seconds":60,"gpu_device_seconds":0}},manifest_sha256="f"*64)
historical=ExecutionRequest.objects.create(job=job,credential=credential,state="authorized")
Attempt.objects.create(execution_request=historical,job=job,connection=connection,grant=grant,state="succeeded",accounting_state="settled",reservation_cpu=0,reservation_gpu=0)
waiting=[]
if mode=="approval_required":
    for index in range(4):
        with transaction.atomic():
            data,_=services.request_execution(job,credential)
        waiting.append(data["id"])
barrier=Barrier(4)
def reserve(index):
    close_old_connections()
    barrier.wait(timeout=15)
    try:
        with transaction.atomic():
            current_job=Job.objects.get(id=job.id)
            if mode=="approval_required":
                services.decide(current_job,User.objects.get(id=owner.id),{"execution_request_id":waiting[index],"manifest_sha256":"f"*64,"approved":True})
            else:
                services.request_execution(current_job,ProjectCredential.objects.get(id=credential.id))
        return "authorized"
    except BrokerError as error:
        if error.code!="TOTAL_JOB_LIMIT": raise
        return error.code
    finally:
        close_old_connections()
with ThreadPoolExecutor(max_workers=4) as executor:
    outcomes=list(executor.map(reserve,range(4)))
grant.refresh_from_db()
assert outcomes.count("authorized")==1 and outcomes.count("TOTAL_JOB_LIMIT")==3,outcomes
expected_reserved=240 if mode=="approval_required" else 60
expected_waiting=3 if mode=="approval_required" else 0
assert grant.reserved_cpu==expected_reserved and Attempt.objects.count()==2
assert LedgerEntry.objects.filter(action="reserve").count()==1
assert ExecutionRequest.objects.filter(reservation_state='held').count()==expected_waiting
if mode=='approval_required':
    assert ExecutionRequest.objects.filter(reservation_state='held',reservation_cpu=60,reservation_gpu=0,state='awaiting_approval').count()==3
    assert ExecutionRequest.objects.filter(reservation_state='converted').count()==1
    assert LedgerEntry.objects.filter(action='hold').count()==4
    assert LedgerEntry.objects.filter(action='hold_convert').count()==1
    assert not LedgerEntry.objects.filter(action='hold_release').exists()
assert services.allowance(project)["jobs"]=={"limit":2,"admitted":2,"remaining":0,"active":1,"active_limit":100,"awaiting_approval":expected_waiting}
print(json.dumps({"authorized":1,"admitted":2,"reserved_cpu":grant.reserved_cpu}))
'''
    environment = dict(os.environ, DJANGO_SETTINGS_MODULE="compute_broker.control.settings",
        PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"), SUBMISSION_LIMIT_TEST_MODE=mode,
        BROKER_DB_ENGINE="sqlite", BROKER_DATA_DIR=str(tmp_path / "service"), BROKER_DB_PATH=str(tmp_path / "race.sqlite3"),
        BROKER_ARTIFACT_ROOT=str(tmp_path / "objects"), BROKER_PROFILE="development")
    result = subprocess.run([sys.executable, "-c", script], env=environment, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "authorized": 1, "admitted": 2, "reserved_cpu": 240 if mode == "approval_required" else 60}
