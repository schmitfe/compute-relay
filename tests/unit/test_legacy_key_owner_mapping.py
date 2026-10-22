"""Explicit legacy-owner verification never infers a user's cluster identity."""

from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from django.utils import timezone

from compute_broker.control.auth import token_hash
from compute_broker.control.models import AuditEvent, Grant, Membership, Project, ProjectCredential, User
from migrate_control_db import MigrationRefused, write_committed_report
from verify_legacy_key_owners import apply_mapping, load_mapping


@pytest.fixture
def quarantined(db):
    owner = User.objects.create_user(username="legacy-owner", password=None)
    member = User.objects.create_user(username="legacy-submitter", password=None)
    foreign = User.objects.create_user(username="foreign-user", password=None)
    project = Project.objects.create(owner=owner, name="Offline owner verification", paused=True)
    Membership.objects.create(project=project, user=member, roles=["submitter"])
    Grant.objects.create(project=project, policy={})
    key = ProjectCredential.objects.create(project=project, name="quarantined old key", token_hash=token_hash("synthetic-legacy-key"),
        prefix="public-id", revoked=True, scopes=["jobs:read", "jobs:execute"], expires_at=timezone.now()+timedelta(days=1))
    event = AuditEvent.objects.create(project=project, actor="migration-0009", action="credential.owner_unresolved",
        object_id=str(key.id), details={"was_revoked": False})
    return owner, member, foreign, project, key, event


@pytest.mark.parametrize("was_revoked", [False, True])
def test_verified_owner_is_explicit_and_original_revocation_is_preserved(quarantined, was_revoked):
    owner, member, _, _, key, event = quarantined
    event.details = {"was_revoked": was_revoked}
    event.save()
    before = (key.token_hash, key.scopes, key.expires_at, key.execution_mode)
    result = apply_mapping({key.id: member.id}, "a" * 64)
    key.refresh_from_db()
    assert key.issued_by_id == member.id and key.revoked is was_revoked
    assert (key.token_hash, key.scopes, key.expires_at, key.execution_mode) == before
    assert result == {"verified_keys": 1, "kept_revoked": int(was_revoked), "mapping_sha256": "a" * 64}
    audit = AuditEvent.objects.get(action="credential.legacy_owner_verified")
    assert audit.actor == "offline-owner-verification"
    assert audit.details == {"issued_by_id": str(member.id), "originally_revoked": was_revoked, "mapping_sha256": "a" * 64}
    with pytest.raises(MigrationRefused, match="unresolved issuing user"):
        apply_mapping({key.id: owner.id}, "b" * 64)


@pytest.mark.parametrize("condition", ["foreign", "missing", "disabled", "viewer", "missing-key", "assigned", "not-revoked", "no-event", "duplicate-event", "bad-event", "unknown-scope", "revoked-after"])
def test_invalid_mapping_cannot_change_key_or_create_verification_audit(quarantined, condition):
    owner, member, foreign, project, key, event = quarantined
    identity = key.id
    selected = member.id
    if condition == "foreign": selected = foreign.id
    elif condition == "missing": selected = uuid4()
    elif condition == "disabled": member.is_active = False; member.save()
    elif condition == "viewer": Membership.objects.filter(user=member).update(roles=["viewer"])
    elif condition == "missing-key": identity = uuid4()
    elif condition == "assigned": key.issued_by = owner; key.save()
    elif condition == "not-revoked": key.revoked = False; key.save()
    elif condition == "no-event": event.delete()
    elif condition == "duplicate-event": event.pk = None; event.save()
    elif condition == "bad-event": event.details = {"was_revoked": "false"}; event.save()
    elif condition == "unknown-scope": key.scopes = ["unknown:execute"]; key.save()
    elif condition == "revoked-after": AuditEvent.objects.create(project=project, actor=str(owner.id), action="credential.revoked", object_id=str(key.id))
    before = (key.issued_by_id, key.revoked)
    with pytest.raises(MigrationRefused):
        apply_mapping({identity: selected}, "c" * 64)
    key.refresh_from_db()
    assert (key.issued_by_id, key.revoked) == before
    assert not AuditEvent.objects.filter(action="credential.legacy_owner_verified").exists()


@pytest.mark.parametrize("action", ["credential.created", "credential.rotated"])
def test_existing_creation_or_rotation_provenance_cannot_be_overwritten(quarantined, action):
    owner, member, _, project, key, _ = quarantined
    AuditEvent.objects.create(project=project, actor=str(owner.id), action=action,
        object_id=str(key.id) if action == "credential.created" else str(uuid4()),
        details={} if action == "credential.created" else {"replacement_id": str(key.id)})
    with pytest.raises(MigrationRefused, match="provenance exists"):
        apply_mapping({key.id: member.id}, "d" * 64)
    key.refresh_from_db()
    assert key.issued_by_id is None and key.revoked


def test_one_invalid_assignment_rolls_back_the_entire_batch(quarantined):
    _, member, _, project, key, _ = quarantined
    second = ProjectCredential.objects.create(project=project, name="second", prefix="second-id", token_hash=token_hash("second-synthetic"),
        revoked=True, scopes=["jobs:read"], expires_at=timezone.now()+timedelta(days=1))
    AuditEvent.objects.create(project=project, actor="migration", action="credential.owner_unresolved", object_id=str(second.id), details={"was_revoked": False})
    with pytest.raises(MigrationRefused):
        apply_mapping({key.id: member.id, second.id: uuid4()}, "e" * 64)
    assert not ProjectCredential.objects.filter(issued_by__isnull=False).exists()
    assert not AuditEvent.objects.filter(action="credential.legacy_owner_verified").exists()


def test_unpaused_projects_cannot_be_bypassed_by_an_explicit_mapping(quarantined):
    _, member, _, project, key, _ = quarantined
    project.paused = False
    project.save()
    with pytest.raises(MigrationRefused, match="Pause every project"):
        apply_mapping({key.id: member.id}, "f" * 64)


def test_mapping_file_is_private_bounded_strict_and_contains_only_uuid_pairs(tmp_path):
    path = tmp_path / "mapping.json"
    key, user = uuid4(), uuid4()
    path.write_text(json.dumps({"schema_version": "1.0", "owners": {str(key): str(user)}}))
    path.chmod(0o600)
    assert load_mapping(path) == {key: user}
    path.chmod(0o644)
    with pytest.raises(MigrationRefused, match="private regular file"):
        load_mapping(path)
    path.chmod(0o600)
    path.write_text('{"schema_version":"1.0","owners":{"bad":"bad"}}')
    with pytest.raises(MigrationRefused, match="UUID string"):
        load_mapping(path)
    path.write_text('{"schema_version":"1.0","owners":{},"owners":{}}')
    with pytest.raises(ValueError, match="Duplicate"):
        load_mapping(path)


def test_committed_database_success_is_not_reported_as_rollback_when_report_io_fails(tmp_path, monkeypatch, capsys):
    import migrate_control_db
    monkeypatch.setattr(migrate_control_db, "private_write", lambda *_: (_ for _ in ()).throw(OSError("synthetic secret must not be logged")))
    assert write_committed_report(tmp_path / "report.json", {"operation": "import"}) is False
    output = capsys.readouterr()
    assert "REPORT_NOT_WRITTEN" in output.err and "synthetic secret" not in output.err


def test_operator_command_reports_only_aggregate_success_and_replay_fails(tmp_path):
    root = Path(__file__).resolve().parents[2]
    tmp_path.chmod(0o700)
    seed = r'''
import json,os,django
django.setup()
from datetime import timedelta
from pathlib import Path
from django.core.management import call_command
from django.utils import timezone
from compute_broker.control.models import User,Project,ProjectCredential,Grant,AuditEvent
call_command('migrate',interactive=False,verbosity=0)
user=User.objects.create_user(username='synthetic-offline-owner',password=None)
project=Project.objects.create(owner=user,name='Synthetic paused project',paused=True);Grant.objects.create(project=project,policy={})
key=ProjectCredential.objects.create(project=project,name='quarantined',token_hash='a'*64,prefix='public',revoked=True,scopes=['jobs:read'],expires_at=timezone.now()+timedelta(days=1))
AuditEvent.objects.create(project=project,actor='migration',action='credential.owner_unresolved',object_id=str(key.id),details={'was_revoked':False})
path=Path(os.environ['MAPPING_TEST_ROOT'])/'mapping.json';path.write_text(json.dumps({'schema_version':'1.0','owners':{str(key.id):str(user.id)}}));path.chmod(0o600)
'''
    environment = {key: value for key, value in os.environ.items() if not key.startswith("BROKER_")}
    environment.update(PYTHONPATH=str(root / "src"), DJANGO_SETTINGS_MODULE="compute_broker.control.settings", BROKER_EXTENSIONS="",
        BROKER_DATA_DIR=str(tmp_path / "service"), BROKER_DB_PATH=str(tmp_path / "offline.sqlite3"), MAPPING_TEST_ROOT=str(tmp_path))
    seeded = subprocess.run([sys.executable, "-c", seed], cwd=root, env=environment, capture_output=True, text=True, timeout=30)
    assert seeded.returncode == 0, seeded.stderr
    arguments = [sys.executable, str(root / "tools/verify_legacy_key_owners.py"), "--mapping", str(tmp_path / "mapping.json"),
        "--report", str(tmp_path / "report.json"), "--offline-confirmed"]
    result = subprocess.run(arguments, cwd=root, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] and report["verified_keys"] == 1 and report["report_written"]
    assert str(tmp_path) not in result.stdout and "cbp_" not in result.stdout and "cbw_" not in result.stdout
    arguments[arguments.index("--report") + 1] = str(tmp_path / "replay-report.json")
    replay = subprocess.run(arguments, cwd=root, env=environment, capture_output=True, text=True, timeout=15)
    assert replay.returncode == 1 and "OWNER_MAPPING_REFUSED" in replay.stderr


def test_0009_uses_creation_or_rotation_provenance_and_quarantines_removed_member_unknowns(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = r'''
import django
django.setup()
from datetime import timedelta
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
call_command('migrate','broker','0008_connection_setup_complete',interactive=False,verbosity=0)
state=MigrationExecutor(connection).loader.project_state([('broker','0008_connection_setup_complete')]).apps
User=state.get_model('broker','User');Project=state.get_model('broker','Project');Credential=state.get_model('broker','ProjectCredential');Audit=state.get_model('broker','AuditEvent');Membership=state.get_model('broker','Membership')
owner=User.objects.create(username='owner');former=User.objects.create(username='removed-former-member')
project=Project.objects.create(owner=owner,name='historically-shared',paused=True)
Membership.objects.create(project=project,user=former,roles=['submitter']);Membership.objects.filter(project=project).delete()
def key(name,revoked=False):
    return Credential.objects.create(project=project,name=name,token_hash=name.ljust(64,'0'),prefix='public',scopes=['jobs:read'],revoked=revoked,expires_at=timezone.now()+timedelta(days=1))
created=key('created');unknown=key('unknown');previously_revoked=key('previously-revoked',True);rotated=key('rotated')
Audit.objects.create(project=project,actor=str(former.id),action='credential.created',object_id=str(created.id))
Audit.objects.create(project=project,actor=str(owner.id),action='credential.rotated',object_id=str(created.id),details={'replacement_id':str(rotated.id)})
call_command('migrate','broker','0010_authentication_identity',interactive=False,verbosity=0)
from compute_broker.control.models import ProjectCredential,AuditEvent
assert ProjectCredential.objects.get(id=created.id).issued_by_id==former.id
assert ProjectCredential.objects.get(id=rotated.id).issued_by_id==owner.id
for identity,was_revoked in [(unknown.id,False),(previously_revoked.id,True)]:
    record=ProjectCredential.objects.get(id=identity)
    assert record.issued_by_id is None and record.revoked is True
    event=AuditEvent.objects.get(object_id=str(identity),action='credential.owner_unresolved')
    assert event.details=={'was_revoked':was_revoked}
assert not AuditEvent.objects.filter(object_id__in=[str(created.id),str(rotated.id)],action='credential.owner_unresolved').exists()
print('provenance-and-quarantine-preserved')
'''
    environment = {key: value for key, value in os.environ.items() if not key.startswith("BROKER_")}
    environment.update(PYTHONPATH=str(root / "src"), DJANGO_SETTINGS_MODULE="compute_broker.control.settings", BROKER_EXTENSIONS="",
        BROKER_DATA_DIR=str(tmp_path / "service"), BROKER_DB_PATH=str(tmp_path / "historical.sqlite3"))
    result = subprocess.run([sys.executable, "-c", script], cwd=root, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and result.stdout.strip() == "provenance-and-quarantine-preserved", result.stderr
