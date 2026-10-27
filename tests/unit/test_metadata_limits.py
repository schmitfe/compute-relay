"""Logical metadata is bounded independently of deduplicated input bytes."""
from datetime import timedelta

import pytest
from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.errors import BrokerError
from compute_relay.control.metadata import compact_idempotency, metadata_usage, reserve_metadata
from compute_relay.control.models import AuditEvent, Blob, IdempotencyRecord, Job, MetadataUsage, Project
from test_control import broker, budgeted, dispatched, prepared_job, submit_receipt


def test_duplicate_blob_drafts_have_independent_count_and_byte_charges(broker, settings):
    job = prepared_job(broker)
    before = Blob.objects.count()
    settings.MAX_PROJECT_DRAFT_JOBS = 1
    with pytest.raises(BrokerError) as exc:
        services.create_job(broker.credential, job.request)
    assert exc.value.code == "METADATA_QUOTA"
    assert Job.objects.count() == 1 and Blob.objects.count() == before
    settings.MAX_PROJECT_DRAFT_JOBS = 100
    settings.PROJECT_METADATA_LIMIT = metadata_usage(broker.project)["logical_bytes"] + 1000
    with pytest.raises(BrokerError) as exc:
        services.create_job(broker.credential, job.request)
    assert exc.value.code == "METADATA_QUOTA"
    assert Job.objects.count() == 1


def test_installation_limits_serialize_different_project_admissions(broker, settings):
    other = Project.objects.create(owner=broker.owner, name="second")
    settings.GLOBAL_METADATA_LIMIT = 10
    reserve_metadata(broker.project, "definition", "first", 6)
    with pytest.raises(BrokerError) as exc:
        reserve_metadata(other, "definition", "second", 5)
    assert exc.value.code == "METADATA_QUOTA"
    assert not MetadataUsage.objects.filter(project=other).exists()
    assert metadata_usage()["logical_bytes"] == 6


def test_idempotency_response_bytes_are_charged_and_action_rolls_back(broker, settings):
    settings.PROJECT_METADATA_LIMIT = 1024
    def action():
        Project.objects.filter(id=broker.project.id).update(name="must roll back")
        return {"large": "x" * 2048}, 200
    with pytest.raises(BrokerError) as exc:
        services.idempotent(broker.project, "large", "once", {}, action)
    assert exc.value.code == "METADATA_QUOTA"
    broker.project.refresh_from_db()
    assert broker.project.name == "synthetic project"
    assert not IdempotencyRecord.objects.exists() and not MetadataUsage.objects.exists()


def test_compaction_retains_key_tombstone_and_cannot_reexecute(broker, settings):
    calls = []
    def action():
        calls.append(True)
        return {"value": "x" * 2000}, 201
    original = services.idempotent(broker.project, "operation", "once", {}, action)
    before = metadata_usage(broker.project)["logical_bytes"]
    settings.MAX_PROJECT_IDEMPOTENCY_KEYS = 1
    assert compact_idempotency(before=timezone.now() + timedelta(seconds=1), limit=1) == 1
    retired = services.idempotent(broker.project, "operation", "once", {}, action)
    assert retired[1] == 409 and retired[0]["error"]["code"] == "IDEMPOTENCY_RETIRED"
    assert calls == [True] and original[1] == 201
    assert metadata_usage(broker.project)["logical_bytes"] < before
    assert services.idempotent(broker.project, "operation", "new", {}, action)[1] == 201
    assert calls == [True, True]
    with pytest.raises(BrokerError) as exc:
        services.idempotent(broker.project, "operation", "another", {}, action)
    assert exc.value.code == "METADATA_QUOTA"
    with pytest.raises(BrokerError) as exc:
        services.idempotent(broker.project, "operation", "once", {"changed": True}, action)
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_audit_retention_bounds_count_and_detail_bytes_without_admission_quota(broker, settings):
    settings.MAX_PROJECT_AUDIT_EVENTS = 3
    settings.MAX_INSTALLATION_AUDIT_EVENTS = 4
    settings.PROJECT_METADATA_LIMIT = 0
    other = Project.objects.create(owner=broker.owner, name="second")
    for index in range(20):
        services.audit(broker.project if index % 2 else other, "actor", "test", index, {"large": "x" * 5000})
    assert AuditEvent.objects.count() == 4
    assert AuditEvent.objects.filter(project=broker.project).count() <= 3
    assert all(row.details["truncated"] for row in AuditEvent.objects.all())


def test_full_metadata_capacity_does_not_block_worker_accounting(broker, settings):
    budgeted(broker)
    job = prepared_job(broker)
    attempt, lease = dispatched(broker, job)
    native, lease = submit_receipt(broker, attempt, lease)
    settings.PROJECT_METADATA_LIMIT = 0
    settings.GLOBAL_METADATA_LIMIT = 0
    services.report(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "outcome": "observed", "native_identity": native, "observation": {"state": "completed", "terminal": True,
        "accounting": {"cpu_core_seconds": 2, "gpu_device_seconds": 0, "final": True}}})
    attempt.refresh_from_db()
    broker.grant.refresh_from_db()
    assert attempt.accounting_state == "settled" and broker.grant.reserved_cpu == 0


def test_workflow_definition_documents_share_definition_capacity(broker, settings):
    from copy import deepcopy
    from compute_relay.extensions.workflows.services import create_revision
    from compute_relay.extensions.workflows.models import WorkflowRevision
    broker.credential.scopes += ["workflows:write"]
    broker.credential.save(update_fields=["scopes"])
    document = {"name": "bounded", "nodes": [{"key": "one", "request": prepared_job(broker).request}]}
    settings.MAX_PROJECT_DEFINITIONS = 1
    create_revision(broker.credential, document)
    with pytest.raises(BrokerError) as exc:
        create_revision(broker.credential, deepcopy(document))
    assert exc.value.code == "METADATA_QUOTA"
    assert WorkflowRevision.objects.count() == 1


def test_installation_byte_ceiling_survives_simultaneous_project_writers(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    script = '''
import django
django.setup()
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.conf import settings
from django.core.management import call_command
from django.db import close_old_connections
from compute_relay.control.models import User, Project, MetadataUsage
from compute_relay.control.metadata import reserve_metadata
from compute_relay.control.errors import BrokerError
call_command('migrate', verbosity=0)
owner=User.objects.create_user(username='metadata-race')
projects=[Project.objects.create(owner=owner,name=str(i)) for i in range(4)]
settings.GLOBAL_METADATA_LIMIT=10
barrier=Barrier(4)
def write(index):
    close_old_connections()
    barrier.wait(timeout=10)
    try:
        reserve_metadata(projects[index],'definition','same-local-id',6)
        return 'stored'
    except BrokerError as error:
        return error.code
    finally:
        close_old_connections()
with ThreadPoolExecutor(max_workers=4) as executor:
    outcomes=list(executor.map(write,range(4)))
assert outcomes.count('stored')==1 and outcomes.count('METADATA_QUOTA')==3,outcomes
assert MetadataUsage.objects.count()==1
print('bounded')
'''
    environment = dict(os.environ, DJANGO_SETTINGS_MODULE="compute_relay.control.settings", BROKER_EXTENSIONS="",
        PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"), BROKER_DATA_DIR=str(tmp_path / "state"),
        BROKER_DB_PATH=str(tmp_path / "race.sqlite3"))
    process = subprocess.run([sys.executable, "-c", script], env=environment, text=True, capture_output=True, timeout=30)
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "bounded"
