"""Approval holds have durable independent anchors and bounded migration claims."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from django.db import IntegrityError, transaction

from compute_broker.control.models import (Attempt, Connection, ExecutionRequest, Grant, Job, LedgerEntry,
    Project, User, WorkerIdentity)
from migrate_control_db import MigrationRefused, quiescent_and_balanced


@pytest.fixture
def approval_records(db):
    owner = User.objects.create_user(username="synthetic-approval-journal")
    project = Project.objects.create(owner=owner, name="Synthetic offline journal", paused=True)
    grant = Grant.objects.create(project=project, policy={})
    worker = WorkerIdentity.objects.create(owner=owner, name="Synthetic journal worker", token_hash="a" * 64)
    connection = Connection.objects.create(owner=owner, worker=worker, name="Synthetic journal cluster", backend="fake",
        configuration={}, revision_sha256="b" * 64, discovery_state="ready")
    job = Job.objects.create(project=project, request={}, manifest={})
    execution = ExecutionRequest.objects.create(job=job, state="rejected")
    return SimpleNamespace(owner=owner, project=project, grant=grant, worker=worker, connection=connection,
        job=job, execution=execution)


@pytest.mark.parametrize("field,value", [("reservation_cpu", -1), ("reservation_gpu", -1), ("reservation_state", "unknown")])
def test_execution_hold_constraints_are_enforced_by_database(approval_records, field, value):
    with pytest.raises(IntegrityError), transaction.atomic():
        ExecutionRequest.objects.filter(id=approval_records.execution.id).update(**{field: value})


def test_ledger_requires_exactly_one_anchor_and_enforces_each_action_once(approval_records):
    context = approval_records
    attempt = Attempt.objects.create(job=context.job, execution_request=context.execution, connection=context.connection,
        grant=context.grant, reservation_cpu=60, reservation_gpu=0)
    for anchors in ({}, {"attempt": attempt, "execution_request": context.execution}):
        with pytest.raises(IntegrityError), transaction.atomic():
            LedgerEntry.objects.create(grant=context.grant, action="hold", cpu=60, gpu=0, **anchors)
    LedgerEntry.objects.create(grant=context.grant, execution_request=context.execution, action="hold", cpu=60, gpu=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        LedgerEntry.objects.create(grant=context.grant, execution_request=context.execution, action="hold", cpu=60, gpu=0)
    LedgerEntry.objects.create(grant=context.grant, attempt=attempt, action="reserve", cpu=60, gpu=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        LedgerEntry.objects.create(grant=context.grant, attempt=attempt, action="reserve", cpu=60, gpu=0)


def finalized_hold(context, *, converted=False):
    execution = context.execution
    execution.reservation_cpu, execution.reservation_gpu = 60, 0
    execution.reservation_state = "converted" if converted else "released"
    execution.state = "authorized" if converted else "rejected"
    execution.approval_requirements = {"project_policy": "approval_required", "api_key": "approval_required"}
    execution.save()
    LedgerEntry.objects.create(grant=context.grant, execution_request=execution, action="hold", cpu=60, gpu=0)
    LedgerEntry.objects.create(grant=context.grant, execution_request=execution,
        action="hold_convert" if converted else "hold_release", cpu=0, gpu=0)
    if converted:
        attempt = Attempt.objects.create(job=context.job, execution_request=execution, connection=context.connection,
            grant=context.grant, state="succeeded", accounting_state="settled", reservation_cpu=60, reservation_gpu=0)
        LedgerEntry.objects.create(grant=context.grant, attempt=attempt, action="reserve", cpu=60, gpu=0)
        LedgerEntry.objects.create(grant=context.grant, attempt=attempt, action="settle", cpu=2, gpu=0)
        context.grant.spent_cpu = 2;context.grant.save()
        return attempt


@pytest.mark.parametrize("converted", [False, True])
def test_offline_balances_accept_finalized_hold_journals_without_double_counting_spent(approval_records, converted):
    context = approval_records
    finalized_hold(context, converted=converted)
    balances = quiescent_and_balanced()
    assert balances == [{"grant": str(context.grant.id), "spent_cpu": 2 if converted else 0,
        "spent_gpu": 0, "reserved_cpu": 0, "reserved_gpu": 0}]


@pytest.mark.parametrize("mutation", ["held", "missing_hold", "missing_final", "wrong_hold_amount", "nonzero_final",
    "both_final_actions", "wrong_grant", "none_with_journal", "none_with_amount", "converted_without_attempt",
    "wrong_attempt_amount", "released_with_attempt"])
def test_offline_migration_refuses_inconsistent_or_nonquiescent_hold_journals(approval_records, mutation):
    context = approval_records
    current_attempt = finalized_hold(context, converted=mutation in {"converted_without_attempt", "wrong_attempt_amount"})
    execution = context.execution
    if mutation == "held":
        execution.reservation_state = "held";execution.save()
    elif mutation == "missing_hold":
        execution.budget_entries.filter(action="hold").delete()
    elif mutation == "missing_final":
        execution.budget_entries.filter(action="hold_release").delete()
    elif mutation == "wrong_hold_amount":
        execution.budget_entries.filter(action="hold").update(cpu=61)
    elif mutation == "nonzero_final":
        execution.budget_entries.filter(action="hold_release").update(cpu=1)
    elif mutation == "both_final_actions":
        LedgerEntry.objects.create(grant=context.grant, execution_request=execution, action="hold_convert", cpu=0, gpu=0)
    elif mutation == "wrong_grant":
        other = Project.objects.create(owner=context.owner, name="Another offline journal", paused=True)
        other_grant = Grant.objects.create(project=other, policy={})
        execution.budget_entries.update(grant=other_grant)
    elif mutation == "none_with_journal":
        execution.reservation_state = "none";execution.save()
    elif mutation == "none_with_amount":
        execution.budget_entries.all().delete();execution.reservation_state = "none";execution.save()
    elif mutation == "converted_without_attempt":
        current_attempt.ledgerentry_set.all().delete();current_attempt.delete()
        context.grant.spent_cpu = 0;context.grant.save()
    elif mutation == "wrong_attempt_amount":
        current_attempt.reservation_cpu = 61;current_attempt.save()
        current_attempt.ledgerentry_set.filter(action="reserve").update(cpu=61)
    else:
        current_attempt = Attempt.objects.create(job=context.job, execution_request=execution, connection=context.connection,
            grant=context.grant, state="cancelled", accounting_state="released", reservation_cpu=60, reservation_gpu=0)
        LedgerEntry.objects.create(grant=context.grant, attempt=current_attempt, action="reserve", cpu=60, gpu=0)
        LedgerEntry.objects.create(grant=context.grant, attempt=current_attempt, action="release", cpu=0, gpu=0)
    with pytest.raises(MigrationRefused):
        quiescent_and_balanced()


LEGACY_MIGRATION_SCRIPT = r'''
import importlib, json, os
from types import SimpleNamespace
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
executor=MigrationExecutor(connection)
executor.migrate([('broker','0015_cluster_checks')])
apps=executor.loader.project_state([('broker','0015_cluster_checks')]).apps
User=apps.get_model('broker','User');Project=apps.get_model('broker','Project');Grant=apps.get_model('broker','Grant')
Worker=apps.get_model('broker','WorkerIdentity');Connection=apps.get_model('broker','Connection')
Job=apps.get_model('broker','Job');Execution=apps.get_model('broker','ExecutionRequest');Attempt=apps.get_model('broker','Attempt')
owner=User.objects.create(username='synthetic-legacy-owner')
project=Project.objects.create(owner=owner,name='Synthetic legacy project')
mode=os.environ['APPROVAL_MIGRATION_CASE']
grant=None if mode=='missing_grant' else Grant.objects.create(project=project,policy={'allowance':{'cpu_core_seconds':1,'gpu_device_seconds':0}},reserved_cpu=7,reserved_gpu=1)
worker=Worker.objects.create(owner=owner,name='Synthetic legacy worker',token_hash='a'*64)
cluster=Connection.objects.create(owner=owner,worker=worker,name='Synthetic legacy cluster',backend='fake',configuration={},revision_sha256='b'*64)
value=json.loads(os.environ.get('APPROVAL_MIGRATION_VALUE','60'))
manifest={'reservation':{'cpu_core_seconds':value,'gpu_device_seconds':2}}
job=Job.objects.create(project=project,request={},manifest=manifest,manifest_sha256='c'*64,state='prepared')
pending=Execution.objects.create(job=job,state='awaiting_approval')
empty=Execution.objects.create(job=Job.objects.create(project=project,request={},manifest={}),state='awaiting_approval')
terminal=Execution.objects.create(job=job,state='rejected')
if mode in {'success','zero'}:
    second=Execution.objects.create(job=job,state='awaiting_approval')
    previous=Execution.objects.create(job=job,state='awaiting_approval')
    Attempt.objects.create(job=job,execution_request=previous,connection=cluster,grant=grant,reservation_cpu=7,reservation_gpu=1)
elif mode=='overflow':
    grant.reserved_cpu=2**63-1;grant.save()
executor=MigrationExecutor(connection)
if mode not in {'success','zero'}:
    try: executor.migrate([('broker','0016_pending_approval_budget')])
    except RuntimeError: pass
    else: raise AssertionError('Invalid pending claim was silently migrated')
    assert not MigrationRecorder(connection).migration_qs.filter(app='broker',name='0016_pending_approval_budget').exists()
    assert Execution.objects.count()==3
    with connection.cursor() as cursor:
        columns={row.name for row in connection.introspection.get_table_description(cursor,'broker_executionrequest')}
    assert 'reservation_cpu' not in columns
    print('legacy-invalid-claim-preserved')
else:
    executor.migrate([('broker','0016_pending_approval_budget')])
    apps=executor.loader.project_state([('broker','0016_pending_approval_budget')]).apps
    Execution=apps.get_model('broker','ExecutionRequest');Grant=apps.get_model('broker','Grant');Ledger=apps.get_model('broker','LedgerEntry')
    for identity in (pending.id,second.id):
        current=Execution.objects.get(id=identity)
        assert current.reservation_state=='held' and (current.reservation_cpu,current.reservation_gpu)==(value,2)
        assert current.approval_requirements=={}
        held=Ledger.objects.get(execution_request_id=identity)
        assert held.action=='hold' and (held.cpu,held.gpu)==(value,2) and held.attempt_id is None
    for identity in (empty.id,terminal.id,previous.id):
        current=Execution.objects.get(id=identity)
        assert current.reservation_state=='none' and current.reservation_cpu==current.reservation_gpu==0
    grant=Grant.objects.get(id=grant.id)
    assert (grant.reserved_cpu,grant.reserved_gpu)==(7+2*value,5)
    assert Ledger.objects.count()==2 and Attempt.objects.count()==1
    # Restarting the data operation cannot double-hold an existing claim.
    importlib.import_module('compute_broker.control.migrations.0016_pending_approval_budget').backfill_pending_holds(apps,SimpleNamespace(connection=connection))
    grant.refresh_from_db()
    assert (grant.reserved_cpu,grant.reserved_gpu)==(7+2*value,5) and Ledger.objects.count()==2
    assert Job.objects.get(id=job.id).manifest==manifest
    print('legacy-approval-holds-migrated')
'''


@pytest.mark.parametrize("mode,value", [("success", 60), ("zero", 0), ("invalid", -1), ("invalid", True), ("invalid", None),
    ("invalid", "60"), ("invalid", 2**53), ("missing_grant", 60), ("overflow", 60)])
def test_pending_approval_data_migration_is_atomic_and_preserves_historical_packages(tmp_path, mode, value):
    root = Path(__file__).resolve().parents[2]
    environment = {key: item for key, item in os.environ.items() if not key.startswith("BROKER_")}
    environment.update(DJANGO_SETTINGS_MODULE="compute_broker.control.settings", BROKER_PROFILE="development",
        PYTHONPATH=str(root / "src"), BROKER_DB_ENGINE="sqlite", BROKER_DB_PATH=str(tmp_path / "legacy.sqlite3"),
        BROKER_DATA_DIR=str(tmp_path / "service"), BROKER_ARTIFACT_ROOT=str(tmp_path / "objects"), BROKER_EXTENSIONS="",
        APPROVAL_MIGRATION_CASE=mode, APPROVAL_MIGRATION_VALUE=json.dumps(value))
    result = subprocess.run([sys.executable, "-c", LEGACY_MIGRATION_SCRIPT], cwd=root, env=environment,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("legacy-approval-holds-migrated" if mode in {"success", "zero"} else "legacy-invalid-claim-preserved")
