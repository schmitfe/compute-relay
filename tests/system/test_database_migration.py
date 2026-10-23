"""Offline database copy acceptance using only synthetic, disposable data."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/migrate_control_db.py"

SEED = r'''
import json,os,sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from django.utils import timezone
from compute_broker.cli import bootstrap_demo
root=Path(os.environ['MIGRATION_TEST_ROOT'])
bootstrap_demo(SimpleNamespace(output=str(root/'demo'),backend='fake',mode='budgeted',fake_state_directory=str(root/'fake')))
from django.contrib.auth.models import Group,Permission
from django.contrib.sessions.models import Session
from django.db import connection,transaction
from compute_broker.control import services
from compute_broker.control.auth import token_hash
from compute_broker.control.models import *
from compute_broker.control.storage import store_blob
from compute_broker.extensions.environments.models import EnvironmentBuildPolicy,EnvironmentSpec,EnvironmentBuild
from compute_broker.extensions.storage.models import *
from compute_broker.extensions.workflows.models import WorkflowRevision,WorkflowRun,WorkflowNodeRun
user=User.objects.get();user.set_password('synthetic-migration-password');user.save()
project=Project.objects.get();credential=ProjectCredential.objects.get();conn=Connection.objects.get();env=Environment.objects.get()
conn.discovery_state='ready';conn.save()
group=Group.objects.create(name='synthetic-group');permission=Permission.objects.first()
User.groups.through.objects.create(id=901,user=user,group=group)
Group.permissions.through.objects.create(id=902,group=group,permission=permission)
User.user_permissions.through.objects.create(id=903,user=user,permission=permission)
Project.connections.through.objects.all().delete();Project.connections.through.objects.create(id=904,project=project,connection=conn)
Project.environments.through.objects.all().delete();Project.environments.through.objects.create(id=905,project=project,environment=env)
stamp=timezone.now().replace(microsecond=123456)
session=Session(session_key='synthetic-migration-session',expire_date=stamp+timedelta(days=1))
session.session_data=Session.objects.encode({'_auth_user_id':str(user.id),'_auth_user_backend':'django.contrib.auth.backends.ModelBackend','_auth_user_hash':user.get_session_auth_hash()});session.save()
source=store_blob(project,b'print("tiny synthetic source")\n')
request={'schema_version':'1.0','project_id':str(project.id),'connection_id':str(conn.id),'name':'migration-fixture','environment':{'id':str(env.id),'revision_sha256':env.revision_sha256},'command':{'interpreter':'python3','script':'main.py','arguments':[]},'resources':{'nodes':1,'cpus':1,'gpus':0,'memory_mib':128,'time_limit_seconds':60},'slurm':{'partition':'debug','account':'broker'},'files':[{'path':'main.py','sha256':source.sha256,'size_bytes':source.size_bytes,'executable':False}],'outputs':['result.json']}
created,_=services.create_job(credential,request);job=Job.objects.get(id=created['id']);services.prepare_job(job);job.refresh_from_db();job.created_at=stamp;job.save()
with transaction.atomic():
    result,_=services.request_execution(job,credential)
    attempt=Attempt.objects.get(id=result['attempt_id']);attempt.state='succeeded';services._settle(attempt,2,0,{'synthetic':True});attempt.save()
ApprovalDecision.objects.create(execution_request=attempt.execution_request,approver=user,approved=True,manifest_sha256=job.manifest_sha256,consumed_at=stamp,comment='Preserve exact approval and microseconds')
output=store_blob(project,b'{"synthetic":true}\n');Artifact.objects.create(attempt=attempt,path='result.json',blob=output)
revised,_=services.create_job(credential,{**request,'name':'migration-rejected-revision'},previous=job)
rejection=ExecutionRequest.objects.create(job_id=revised['id'],credential=credential,state='rejected')
ApprovalDecision.objects.create(execution_request=rejection,approver=user,approved=False,manifest_sha256='a'*64,comment='Synthetic rejected revision')
# Finalized approval holds have their own journal anchors and must survive
# offline transfer without being counted again as settled usage.
converted=attempt.execution_request
converted.reservation_cpu=attempt.reservation_cpu;converted.reservation_gpu=attempt.reservation_gpu
converted.reservation_state='converted';converted.approval_requirements={'project_policy':'approval_required'};converted.save()
LedgerEntry.objects.create(grant=attempt.grant,execution_request=converted,action='hold',cpu=converted.reservation_cpu,gpu=converted.reservation_gpu)
LedgerEntry.objects.create(grant=attempt.grant,execution_request=converted,action='hold_convert',cpu=0,gpu=0)
rejection.reservation_cpu=120;rejection.reservation_gpu=0;rejection.reservation_state='released'
rejection.approval_requirements={'api_key':'approval_required'};rejection.save()
LedgerEntry.objects.create(grant=attempt.grant,execution_request=rejection,action='hold',cpu=120,gpu=0)
LedgerEntry.objects.create(grant=attempt.grant,execution_request=rejection,action='hold_release',cpu=0,gpu=0)
old=ProjectCredential.objects.create(project=project,name='old-key',token_hash=token_hash('synthetic-rotated-project-key'),prefix='old-public-id',expires_at=stamp+timedelta(days=1),revoked=True,replaced_by=credential)
Membership.objects.create(project=project,user=user,roles=['viewer'])
IdempotencyRecord.objects.create(project=project,operation='synthetic',key='migration-fixture',request_sha256='a'*64,response={'job':str(job.id)},status=201)
WorkerReport.objects.create(lease_id=__import__('uuid').uuid4(),attempt=attempt,generation=1,payload_sha256='b'*64,response={'accepted':True},extension_data={'synthetic':{}})
ClusterRegistration.objects.create(owner=user,name='draft-only',host='unbound.invalid')
policy=EnvironmentBuildPolicy.objects.create(project=project,configuration={'synthetic':True},updated_by=user)
spec=EnvironmentSpec.objects.create(project=project,connection=conn,base_environment=env,document={'synthetic':True},sha256='d'*64)
EnvironmentBuild.objects.create(spec=spec,policy=policy,policy_revision=1,policy_sha256='e'*64,credential=credential,environment=env,state='ready',receipt={'synthetic':True})
pool=StorageCapacityPool.objects.create(owner=user,connection=conn,name='synthetic-pool',probe_root='/synthetic/jobs')
target=StorageTarget.objects.create(owner=user,connection=conn,pool=pool,name='synthetic-target',root_alias='work',root='/synthetic/jobs',roles=['work'],capabilities={'synthetic':True},profile_revision_sha256='1'*64,connection_revision_sha256=conn.revision_sha256)
ProjectStorageBudget.objects.create(project=project,bytes_budget=1024,inodes_budget=10)
ProjectStorageBinding.objects.create(project=project,connection=conn,work=target,results=target)
StorageEstimateReservation.objects.create(project=project,target=target,reference=__import__('uuid').uuid4(),bytes_estimate=16,inodes_estimate=1)
StorageObservationLease.objects.create(pool=pool,target=target,generation=1,profile_revision_sha256='1'*64,connection_revision_sha256=conn.revision_sha256,state='reported',expires_at=stamp,reported_at=stamp,response={'synthetic':True})
revision=WorkflowRevision.objects.create(project=project,name='synthetic-flow',document={'name':'synthetic-flow','nodes':[]},sha256='2'*64)
WorkflowRevision.connections.through.objects.create(id=906,workflowrevision=revision,connection=conn)
run=WorkflowRun.objects.create(revision=revision,credential=credential,state='succeeded')
WorkflowNodeRun.objects.create(run=run,key='done',state='succeeded',job=job,execution_request=attempt.execution_request,resolved_inputs=[{'synthetic':True}])
project.refresh_from_db();project.paused=True;project.save()
connection.close()
source=sqlite3.connect(root/'source.sqlite3');backup=sqlite3.connect(root/'backup.sqlite3');source.backup(backup);backup.close();source.close()
os.chmod(root/'backup.sqlite3',0o400)
print(json.dumps({'user':str(user.id),'password':user.password,'job':str(job.id),'stamp':stamp.isoformat(),'session_data':session.session_data}))
'''


def environment(root):
    result = {key: value for key, value in os.environ.items()
              if not key.startswith("BROKER_") and key != "DJANGO_SETTINGS_MODULE"}
    result.update(PYTHONPATH=str(ROOT / "src"), BROKER_DATA_DIR=str(root / "service"),
                  BROKER_DB_PATH=str(root / "source.sqlite3"), BROKER_ARTIFACT_ROOT=str(root / "objects"),
                  BROKER_EXTENSIONS="storage,environments,workflows", MIGRATION_TEST_ROOT=str(root))
    return result


@pytest.fixture
def migration_source(tmp_path):
    tmp_path.chmod(0o700)
    result = subprocess.run([sys.executable, "-c", SEED], cwd=ROOT, env=environment(tmp_path),
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    return tmp_path, json.loads(result.stdout.splitlines()[-1])


def run_tool(root, arguments, *, success=True):
    result = subprocess.run([sys.executable, str(TOOL), *arguments], cwd=ROOT, env=environment(root),
                            capture_output=True, text=True, timeout=45)
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    assert "synthetic-migration-password" not in result.stdout + result.stderr
    assert "cbp_" not in result.stdout + result.stderr and "cbw_" not in result.stdout + result.stderr
    return result


def export(root, output="transfer.json", *, success=True):
    return run_tool(root, ["export", "--sqlite-backup", str(root / "backup.sqlite3"), "--output", str(root / output),
        "--secret-key-file", str(root / "service/django-secret"), "--artifact-root", str(root / "objects"), "--offline-confirmed"], success=success)


def rehearse(root, *, success=True):
    return run_tool(root, ["rehearse-sqlite", "--input", str(root / "transfer.json"), "--output-db", str(root / "copy.sqlite3"),
        "--report", str(root / "report.json"), "--secret-key-file", str(root / "service/django-secret"),
        "--artifact-root", str(root / "objects"), "--offline-confirmed"], success=success)


def mutate_backup(root, sql, parameters=()):
    path = root / "backup.sqlite3"
    path.chmod(0o600)
    with sqlite3.connect(path) as db:
        db.execute(sql, parameters)
    path.chmod(0o400)


def test_rehearsal_preserves_all_tables_authentication_microseconds_and_through_pks(migration_source):
    root, expected = migration_source
    original = hashlib.sha256((root / "backup.sqlite3").read_bytes()).hexdigest()
    export(root)
    assert (root / "transfer.json").stat().st_mode & 0o777 == 0o600
    rehearse(root)
    bundle = json.loads((root / "transfer.json").read_text())
    assert all(bundle["tables"][name]["count"] for name in ["broker_user", "broker_job", "broker_approvaldecision",
        "broker_ledgerentry", "broker_artifact", "broker_workerreport", "django_session",
        "broker_workflows_workflowrevision_connections", "broker_environments_environmentbuild", "broker_storage_storagetarget"])
    assert hashlib.sha256((root / "backup.sqlite3").read_bytes()).hexdigest() == original
    with sqlite3.connect(root / "copy.sqlite3") as db:
        user = db.execute("SELECT id,password FROM broker_user").fetchone()
        assert user == (expected["user"].replace("-", ""), expected["password"])
        assert db.execute("SELECT session_data FROM django_session").fetchone()[0] == expected["session_data"]
        assert db.execute("SELECT created_at FROM broker_job WHERE id=?", (expected["job"].replace("-", ""),)).fetchone()[0].endswith("123456")
        assert db.execute("SELECT id FROM broker_project_connections").fetchone()[0] == 904
        assert db.execute("SELECT id FROM broker_user_groups").fetchone()[0] == 901
        assert db.execute("SELECT spent_cpu,reserved_cpu FROM broker_grant").fetchone() == (2, 0)
        assert db.execute("SELECT count(*) FROM broker_ledgerentry WHERE execution_request_id IS NOT NULL AND attempt_id IS NULL").fetchone()[0] == 4
        assert db.execute("SELECT reservation_cpu,reservation_state,approval_requirements FROM broker_executionrequest WHERE state='rejected'").fetchone() == (120, "released", '{"api_key": "approval_required"}')
    assert json.loads((root / "report.json").read_text())["ok"] is True
    check = r'''
import os
from pathlib import Path
from migrate_control_db import configure_sqlite,secret_value
root=Path(os.environ['MIGRATION_TEST_ROOT'])
configure_sqlite(root/'copy.sqlite3','storage,environments,workflows',secret_value(root/'service/django-secret'),immutable=True)
from compute_broker.control.models import User
from django.contrib.sessions.models import Session
user=User.objects.get()
assert user.check_password('synthetic-migration-password')
session=Session.objects.get().get_decoded()
assert session['_auth_user_id']==str(user.id) and session['_auth_user_hash']==user.get_session_auth_hash()
print('authentication-preserved')
'''
    result = subprocess.run([sys.executable, "-c", check], cwd=ROOT,
        env={**environment(root), "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT / "tools")])},
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and result.stdout.strip() == "authentication-preserved", result.stderr


@pytest.mark.parametrize("sql,parameters", [
    ("UPDATE broker_project SET paused=0", ()),
    ("UPDATE broker_attempt SET state='submission_uncertain'", ()),
    ("UPDATE broker_attempt SET accounting_state='settlement_pending'", ()),
    ("UPDATE broker_grant SET spent_cpu=99", ()),
    ("UPDATE broker_project SET storage_reserved=1", ()),
    ("UPDATE broker_connection SET discovery_state='queued'", ()),
    ("UPDATE broker_executionrequest SET state='awaiting_approval' WHERE state='rejected'", ()),
    ("UPDATE broker_uploadreservation SET state='reserved'", ()),
    ("UPDATE broker_workflows_workflowrun SET state='waiting_budget'", ()),
    ("UPDATE broker_environments_environmentbuild SET state='awaiting_finalization'", ()),
    ("UPDATE broker_storage_storageobservationlease SET state='leased'", ()),
    ("UPDATE broker_executionrequest SET reservation_state='held' WHERE state='rejected'", ()),
    ("DELETE FROM broker_ledgerentry WHERE action='hold_release'", ()),
    ("UPDATE broker_ledgerentry SET cpu=999 WHERE action='hold'", ()),
    ("UPDATE broker_ledgerentry SET cpu=1 WHERE action='hold_convert'", ()),
])
def test_unsafe_or_inconsistent_source_is_never_exported(migration_source, sql, parameters):
    root, _ = migration_source
    mutate_backup(root, sql, parameters)
    result = export(root, success=False)
    assert "MIGRATION_REFUSED" in result.stderr and not (root / "transfer.json").exists()


def test_export_rejects_sidecars_unknown_tables_and_mismatched_module_inventory(migration_source):
    root, _ = migration_source
    (root / "backup.sqlite3-wal").write_bytes(b"synthetic")
    assert "standalone SQLite backup" in export(root, success=False).stderr
    (root / "backup.sqlite3-wal").unlink()
    mutate_backup(root, "CREATE TABLE unknown_private_table (id INTEGER)")
    assert "tables do not match" in export(root, success=False).stderr


def test_corrupt_export_and_different_secret_are_rejected_without_data_import(migration_source):
    root, _ = migration_source
    export(root)
    original = (root / "transfer.json").read_bytes()
    bundle = json.loads(original)
    bundle["records"][0]["pk"] = 999999
    (root / "transfer.json").write_text(json.dumps(bundle))
    assert "checksum failed" in rehearse(root, success=False).stderr
    assert not (root / "copy.sqlite3").exists()
    (root / "transfer.json").write_bytes(original)
    secret = root / "service/django-secret"
    secret.write_text("different-synthetic-secret-" * 4)
    assert "Preserve the original Django secret" in rehearse(root, success=False).stderr
    with sqlite3.connect(root / "copy.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM broker_user").fetchone()[0] == 0


def test_missing_artifact_rolls_back_entire_target_and_does_not_overwrite_existing_outputs(migration_source):
    root, _ = migration_source
    export(root)
    blob = next((root / "objects").glob("*/*"))
    blob.unlink()
    rehearse(root, success=False)
    with sqlite3.connect(root / "copy.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM broker_user").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM auth_permission").fetchone()[0] > 0
    assert "Output already exists" in rehearse(root, success=False).stderr
    assert "Output already exists" in export(root, success=False).stderr


def test_normal_import_refuses_sqlite_target_and_never_logs_connection_details(migration_source):
    root, _ = migration_source
    export(root)
    result = run_tool(root, ["import", "--input", str(root / "transfer.json"), "--report", str(root / "refused.json"),
        "--secret-key-file", str(root / "service/django-secret"), "--artifact-root", str(root / "objects"), "--offline-confirmed"], success=False)
    assert "Import requires PostgreSQL" in result.stderr and not (root / "refused.json").exists()


@pytest.mark.parametrize("target_state", ["user", "group", "session", "permission"])
def test_nonempty_or_modified_bootstrap_target_is_rejected_without_deleting_rows(migration_source, target_state):
    root, _ = migration_source
    export(root)
    script = r'''
import os
from pathlib import Path
from types import SimpleNamespace
from migrate_control_db import configure_sqlite,secret_value,load_bundle,import_database,MigrationRefused
root=Path(os.environ['MIGRATION_TEST_ROOT'])
configure_sqlite(root/'occupied.sqlite3','storage,environments,workflows',secret_value(root/'service/django-secret'),immutable=False)
from django.core.management import call_command
from django.contrib.auth.models import Group,Permission
from django.contrib.sessions.models import Session
from django.utils import timezone
from compute_broker.control.models import User
call_command('migrate',interactive=False,verbosity=0)
kind=os.environ['MIGRATION_TARGET_STATE']
if kind=='user': User.objects.create_user(username='must-survive-refused-import')
elif kind=='group': Group.objects.create(name='must-survive-refused-import')
elif kind=='session': Session.objects.create(session_key='must-survive-refused-import',session_data='synthetic',expire_date=timezone.now())
else: Permission.objects.create(content_type=Permission.objects.first().content_type,codename='unexpected_permission',name='Unexpected permission')
models=[User,Group,Session,Permission];before=[model.objects.count() for model in models]
try:
    import_database(load_bundle(root/'transfer.json'),SimpleNamespace(secret_key_file=root/'service/django-secret',artifact_root=root/'objects'),rehearsal=True)
except MigrationRefused: pass
else: raise AssertionError('occupied target was accepted')
assert before==[model.objects.count() for model in models]
print('existing-target-preserved')
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
        env={**environment(root), "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT / "tools")]), "MIGRATION_TARGET_STATE": target_state},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and result.stdout.strip() == "existing-target-preserved", result.stderr


def test_exports_reject_public_input_permissions_and_existing_or_symlink_outputs(migration_source):
    root, _ = migration_source
    backup = root / "backup.sqlite3"
    backup.chmod(0o644)
    assert "private, non-symlink regular file" in export(root, success=False).stderr
    backup.chmod(0o400)
    (root / "transfer.json").symlink_to(root / "do-not-create.json")
    assert "Output already exists" in export(root, success=False).stderr
    assert not (root / "do-not-create.json").exists()


def test_canonical_json_is_portable_across_jsonb_order_and_negative_zero():
    from migrate_control_db import canonical
    assert canonical({"z": -0.0, "a": [2**63 - 1, {"b": 1, "a": 2}]}) == canonical(
        {"a": [2**63 - 1, {"a": 2, "b": 1}], "z": 0})
    with pytest.raises(ValueError):
        canonical({"invalid": float("nan")})
