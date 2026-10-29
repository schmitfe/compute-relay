"""Control-plane invariants with synthetic credentials and disposable storage."""
from test_support.unit_test_control import broker, budgeted, decide, dispatched, execution, prepared_job, submit_receipt

import base64
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import transaction
from django.test import Client
from django.utils import timezone

from compute_relay.backends import verify_package
from compute_relay.control import services
from compute_relay.control.auth import token_hash
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import (
    ApprovalDecision, Artifact, Attempt, Blob, Connection, Environment,
    ExecutionRequest, Grant, Job, LedgerEntry, Project, ProjectCredential,
    StoragePool, UploadReservation, User, WorkerIdentity,
)
from compute_relay.control.storage import read_blob, store_blob, store_uploaded_blob






@pytest.mark.django_db(transaction=True)
def test_prepare_commits_upload_reservations_outside_idempotency_transaction(broker, monkeypatch):
    from django.db import connection
    source_job = prepared_job(broker)
    created, _ = services.create_job(broker.credential, source_job.request)
    nesting = []
    original = services.store_blob

    def checked_store(*args, **kwargs):
        nesting.append(connection.in_atomic_block)
        return original(*args, **kwargs)

    monkeypatch.setattr(services, "store_blob", checked_store)
    response = Client().post(f"/api/v1/jobs/{created['id']}/prepare", data="{}", content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer " + broker.token, HTTP_IDEMPOTENCY_KEY="prepare-crash-safety")
    assert response.status_code == 200
    assert nesting == [False, False]












def test_reviewed_package_matches_every_stored_byte(broker):
    job = prepared_job(broker)
    package = services.serialize_job(job, package=True)
    files = {entry.path: read_blob(entry.blob) for entry in job.files.select_related("blob")}
    verify_package(package["manifest"], package["manifest_sha256"], files)
    assert files["main.py"].decode() == next(f["text"] for f in package["files"] if f["path"] == "main.py")
    assert "#SBATCH --time=1" in files["_broker/batch.sh"].decode()
    assert package["manifest"]["connection_revision_sha256"] == broker.connection.revision_sha256
    assert not Attempt.objects.exists()


def test_rejection_comment_returns_to_agent_without_authorizing_execution(broker):
    job = prepared_job(broker)
    request = execution(broker, job)
    decide(broker, job, request, approved=False, comment="Use a smaller CPU test first.")
    serialized = services.serialize_job(job)
    assert serialized["execution_requests"][0]["comment"] == "Use a smaller CPU test first."
    assert serialized["execution_requests"][0]["state"] == "rejected"
    assert not Attempt.objects.exists()
    assert list(request.budget_entries.order_by("created_at").values_list("action", flat=True)) == ["hold", "hold_release"]
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == broker.grant.reserved_gpu == 0
    assert not LedgerEntry.objects.filter(attempt__isnull=False).exists()
    with pytest.raises(BrokerError, match="already has a decision"):
        decide(broker, job, request)


def test_digest_mismatch_and_previous_decision_cannot_authorize_new_attempt(broker):
    job = prepared_job(broker)
    request = execution(broker, job)
    with pytest.raises(BrokerError) as error:
        decide(broker, job, request, manifest_sha256="0" * 64)
    assert error.value.code == "REVISION_CHANGED"
    assert not ApprovalDecision.objects.exists()
    decide(broker, job, request)
    assert ApprovalDecision.objects.get(execution_request=request).consumed_at is not None
    assert Attempt.objects.count() == 1
    another = execution(broker, job)
    assert another.state == "awaiting_approval"
    assert not Attempt.objects.filter(execution_request=another).exists()


def test_decision_endpoint_denies_agent_bearer_and_accepts_owner_session(broker):
    job = prepared_job(broker)
    request = execution(broker, job)
    document = {"execution_request_id": str(request.id), "manifest_sha256": job.manifest_sha256, "approved": True}
    agent = Client()
    response = agent.post(f"/api/v1/jobs/{job.id}/decisions", data=json.dumps(document),
                          content_type="application/json", HTTP_AUTHORIZATION="Bearer " + broker.token,
                          HTTP_IDEMPOTENCY_KEY="agent-cannot-approve")
    assert response.status_code in {401, 403}
    assert not ApprovalDecision.objects.exists()
    human = Client()
    human.force_login(broker.owner)
    response = human.post(f"/api/v1/jobs/{job.id}/decisions", data=json.dumps(document),
                          content_type="application/json", HTTP_IDEMPOTENCY_KEY="owner-approves")
    assert response.status_code == 200, response.content
    assert Attempt.objects.count() == 1


def test_outsider_human_cannot_approve_another_project(broker):
    job = prepared_job(broker)
    request = execution(broker, job)
    with pytest.raises(BrokerError) as error:
        with transaction.atomic():
            services.decide(job, broker.outsider, {"execution_request_id": str(request.id),
                            "manifest_sha256": job.manifest_sha256, "approved": True})
    assert error.value.status == 403
    assert not Attempt.objects.exists()


def test_human_decision_requires_csrf_even_with_valid_owner_session(broker):
    job = prepared_job(broker)
    request = execution(broker, job)
    human = Client(enforce_csrf_checks=True)
    human.force_login(broker.owner)
    document = {"execution_request_id": str(request.id), "manifest_sha256": job.manifest_sha256, "approved": False}
    denied = human.post(f"/api/v1/jobs/{job.id}/decisions", data=json.dumps(document),
                        content_type="application/json", HTTP_IDEMPOTENCY_KEY="csrf-required")
    assert denied.status_code == 403
    assert not ApprovalDecision.objects.exists()
    page = human.get(f"/review/{job.id}/")
    assert page.status_code == 200
    accepted = human.post(f"/api/v1/jobs/{job.id}/decisions", data=json.dumps(document),
                          content_type="application/json", HTTP_IDEMPOTENCY_KEY="csrf-present",
                          HTTP_X_CSRFTOKEN=human.cookies["csrftoken"].value)
    assert accepted.status_code == 200, accepted.content


def test_project_and_worker_credentials_have_distinct_authority(broker):
    client = Client()
    denied = client.post("/internal/v1/lease", data="{}", content_type="application/json",
                         HTTP_AUTHORIZATION="Bearer " + broker.token)
    assert denied.status_code == 401
    denied = client.get("/api/v1/connections", HTTP_AUTHORIZATION="Bearer synthetic-worker-token")
    assert denied.status_code == 401


def test_artifact_read_scope_is_separate_from_job_metadata_scope(broker):
    budgeted(broker)
    job = prepared_job(broker)
    attempt = execution(broker, job).attempt
    blob = store_blob(broker.project, b"synthetic artifact")
    artifact = Artifact.objects.create(attempt=attempt, path="results.txt", blob=blob)
    ProjectCredential.objects.create(project=broker.project, issued_by=broker.project.owner, name="metadata only", prefix="metadata",
                                     token_hash=token_hash("metadata-only"), scopes=["jobs:read"],
                                     expires_at=timezone.now() + timedelta(days=1))
    ProjectCredential.objects.create(project=broker.project, issued_by=broker.project.owner, name="artifact reader", prefix="artifact",
                                     token_hash=token_hash("artifact-only"), scopes=["artifacts:read"],
                                     expires_at=timezone.now() + timedelta(days=1))
    client = Client()
    assert client.get(f"/api/v1/jobs/{job.id}", HTTP_AUTHORIZATION="Bearer metadata-only").status_code == 200
    assert client.get(f"/api/v1/artifacts/{artifact.id}/content",
                      HTTP_AUTHORIZATION="Bearer metadata-only").status_code == 403
    content = client.get(f"/api/v1/artifacts/{artifact.id}/content", HTTP_AUTHORIZATION="Bearer artifact-only")
    assert content.status_code == 200
    assert content.content == b"synthetic artifact"
    assert content["X-Content-Type-Options"] == "nosniff"
    assert content["Content-Disposition"].startswith("attachment")


def test_foreign_project_job_and_artifact_ids_do_not_grant_access(broker):
    budgeted(broker)
    job = prepared_job(broker)
    attempt = execution(broker, job).attempt
    artifact = Artifact.objects.create(attempt=attempt, path="results.txt",
                                       blob=store_blob(broker.project, b"private synthetic output"))
    foreign = Project.objects.create(owner=broker.outsider, name="foreign")
    ProjectCredential.objects.create(project=foreign, issued_by=foreign.owner, name="foreign", prefix="foreign",
                                     token_hash=token_hash("foreign-token"), scopes=["jobs:read", "artifacts:read"],
                                     expires_at=timezone.now() + timedelta(days=1))
    client = Client()
    for path in (f"/api/v1/jobs/{job.id}", f"/api/v1/jobs/{job.id}/package",
                 f"/api/v1/attempts/{attempt.id}/logs", f"/api/v1/attempts/{attempt.id}/artifacts",
                 f"/api/v1/artifacts/{artifact.id}/content"):
        response = client.get(path, HTTP_AUTHORIZATION="Bearer foreign-token")
        assert response.status_code == 404, (path, response.status_code)


def test_stable_idempotency_key_reserves_and_creates_attempt_once(broker):
    budgeted(broker)
    job = prepared_job(broker)
    action = lambda: services.request_execution(job, broker.credential)
    first = services.idempotent(broker.project, "execute", "same-request", {"job": str(job.id)}, action)
    second = services.idempotent(broker.project, "execute", "same-request", {"job": str(job.id)}, action)
    assert first == second
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 60
    assert Attempt.objects.count() == LedgerEntry.objects.filter(action="reserve").count() == 1
    with pytest.raises(BrokerError) as error:
        services.idempotent(broker.project, "execute", "same-request", {"job": str(uuid4())}, action)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert Attempt.objects.count() == 1


def test_separate_project_keys_share_one_allowance(broker):
    budgeted(broker, cpu=60, gpu=0)
    job = prepared_job(broker)
    execution(broker, job)
    other = ProjectCredential.objects.create(project=broker.project, issued_by=broker.project.owner, name="another agent", prefix="other",
                                              token_hash=token_hash("another-synthetic-token"), scopes=["jobs:submit"],
                                              expires_at=timezone.now() + timedelta(days=1))
    with pytest.raises(BrokerError) as error:
        execution(broker, job, other)
    assert error.value.code == "BUDGET_UNAVAILABLE"
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 60
    assert Attempt.objects.count() == ExecutionRequest.objects.count() == 1


def test_unknown_submission_preserves_reservation_and_leases_reconcile(broker):
    budgeted(broker)
    job = prepared_job(broker)
    attempt, lease = dispatched(broker, job)
    services.report(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
                                               "outcome": "submission_uncertain"})
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 60 and broker.grant.spent_cpu == 0
    Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now() - timedelta(seconds=1))
    assert services.lease_work(broker.worker)["lease"]["operation"] == "reconcile"


def test_final_overage_is_charged_once_and_exhausted_allowance_blocks_new_admissions(broker):
    budgeted(broker, cpu=60, gpu=0)
    job = prepared_job(broker)
    attempt, lease = dispatched(broker, job)
    native, lease = submit_receipt(broker, attempt, lease)
    data = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "observed",
            "native_identity": native, "observation": {"state": "completed", "terminal": True,
             "accounting": {"cpu_core_seconds": 120, "gpu_device_seconds": 0, "final": True}}}
    response = services.report(broker.worker, attempt.id, data)
    assert services.report(broker.worker, attempt.id, data) == response
    broker.project.refresh_from_db()
    broker.grant.refresh_from_db()
    attempt.refresh_from_db()
    assert not broker.project.paused
    assert (broker.grant.spent_cpu, broker.grant.reserved_cpu) == (120, 0)
    assert attempt.error_code == "OBSERVED_OVERAGE"
    assert services.allowance(broker.project)["debt"]["cpu_core_seconds"] == 60
    assert LedgerEntry.objects.filter(attempt=attempt, action="settle").count() == 1
    with pytest.raises(BrokerError) as error:
        with transaction.atomic():
            services.request_execution(job, broker.credential)
    assert error.value.code == "BUDGET_UNAVAILABLE"


def test_output_storage_failure_does_not_suppress_final_accounting(broker, settings):
    budgeted(broker)
    job = prepared_job(broker, outputs=["result.json"])
    attempt, lease = dispatched(broker, job)
    native, lease = submit_receipt(broker, attempt, lease)
    data = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "observed", "native_identity": native,
            "collection_state": "failed", "error_code": "OUTPUT_COLLECTION_FAILED",
            "observation": {"state": "completed", "terminal": True,
                            "accounting": {"cpu_core_seconds": 2, "gpu_device_seconds": 0, "final": True}}}
    settings.PROJECT_STORAGE_LIMIT = 1
    response = Client(HTTP_AUTHORIZATION="Bearer synthetic-worker-token").post(
        f"/internal/v1/attempts/{attempt.id}/output-chunk", data=json.dumps({
            "lease_id": lease["id"], "generation": lease["generation"], "path": "result.json",
            "sha256": "a" * 64, "size_bytes": 16}), content_type="application/json")
    assert response.status_code == 409
    response = services.report(broker.worker, attempt.id, data)
    broker.grant.refresh_from_db()
    assert response["attempt"]["state"] == "succeeded"
    assert response["attempt"]["accounting_state"] == "settled"
    assert response["attempt"]["error_code"] == "OUTPUT_COLLECTION_FAILED"
    assert broker.grant.spent_cpu == 2 and broker.grant.reserved_cpu == 0


def test_observe_cannot_claim_rejection_and_refund_a_running_native_job(broker):
    budgeted(broker)
    job = prepared_job(broker)
    attempt, lease = dispatched(broker, job)
    _, lease = submit_receipt(broker, attempt, lease)
    with pytest.raises(BrokerError):
        services.report(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
                                                   "outcome": "rejected"})
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 60
    assert not LedgerEntry.objects.filter(action="release").exists()


def test_dispatch_rechecks_revoked_requesting_credential(broker):
    budgeted(broker)
    job = prepared_job(broker)
    request = execution(broker, job)
    lease = services.lease_work(broker.worker)["lease"]
    broker.credential.revoked = True
    broker.credential.save(update_fields=["revoked"])
    with pytest.raises(BrokerError) as error:
        services.dispatch(broker.worker, request.attempt.id,
                          {"lease_id": lease["id"], "generation": lease["generation"],
                           "manifest_sha256": job.manifest_sha256,
                           "connection_revision_sha256": broker.connection.revision_sha256})
    assert error.value.status == 403
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 60


def test_cancel_before_dispatch_invalidates_previously_issued_submit_lease(broker):
    budgeted(broker)
    job = prepared_job(broker)
    request = execution(broker, job)
    lease = services.lease_work(broker.worker)["lease"]
    services.cancel_attempt(request.attempt)
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 0
    with pytest.raises(BrokerError):
        services.dispatch(broker.worker, request.attempt.id,
                          {"lease_id": lease["id"], "generation": lease["generation"],
                           "manifest_sha256": job.manifest_sha256,
                           "connection_revision_sha256": broker.connection.revision_sha256})
    request.attempt.refresh_from_db()
    assert not request.attempt.dispatch_started
    assert request.attempt.state == "cancelled"
    assert request.attempt.accounting_state == "released"


def test_dispatch_rechecks_tightened_policy_after_admission(broker):
    budgeted(broker)
    job = prepared_job(broker)
    request = execution(broker, job)
    lease = services.lease_work(broker.worker)["lease"]
    broker.grant.policy = {**broker.grant.policy, "revision": 2, "allowed_connection_ids": [str(uuid4())]}
    broker.grant.save(update_fields=["policy"])
    with pytest.raises(BrokerError):
        services.dispatch(broker.worker, request.attempt.id,
                          {"lease_id": lease["id"], "generation": lease["generation"],
                           "manifest_sha256": job.manifest_sha256,
                           "connection_revision_sha256": broker.connection.revision_sha256})
    request.attempt.refresh_from_db()
    assert not request.attempt.dispatch_started


def test_project_quota_and_dedup_preserve_exact_storage_counters(broker, settings):
    settings.PROJECT_STORAGE_LIMIT = 16
    first = store_blob(broker.project, b"12345")
    assert store_blob(broker.project, b"12345").id == first.id
    with pytest.raises(BrokerError) as error:
        store_blob(broker.project, b"x" * 12)
    assert error.value.code == "STORAGE_QUOTA"
    broker.project.refresh_from_db()
    pool = StoragePool.objects.get(id=1)
    assert (broker.project.storage_used, broker.project.storage_reserved, broker.project.object_count) == (5, 0, 1)
    assert (pool.used, pool.reserved) == (5, 0)


def test_global_quota_applies_across_projects(broker, settings):
    settings.GLOBAL_STORAGE_LIMIT = 8
    store_blob(broker.project, b"12345")
    other = Project.objects.create(name="other", owner=broker.owner)
    with pytest.raises(BrokerError) as error:
        store_blob(other, b"abcd")
    assert error.value.code == "STORAGE_QUOTA"
    other.refresh_from_db()
    assert other.storage_used == other.storage_reserved == 0
    assert Blob.objects.filter(project=other).count() == 0


def test_failed_disk_write_releases_persistent_upload_reservation(broker):
    original = os.open
    def fail_temporary(path, *args, **kwargs):
        if Path(path).name.startswith("upload-"):
            raise OSError("synthetic disk failure")
        return original(path, *args, **kwargs)
    with patch("compute_relay.control.storage.os.open", side_effect=fail_temporary):
        with pytest.raises(OSError):
            store_blob(broker.project, b"source")
    broker.project.refresh_from_db()
    pool = StoragePool.objects.get(id=1)
    assert broker.project.storage_used == broker.project.storage_reserved == 0
    assert pool.used == pool.reserved == 0
    assert list(UploadReservation.objects.values_list("state", flat=True)) == ["aborted"]


def test_inline_artifacts_are_rejected_without_storing_bytes(broker):
    budgeted(broker)
    job = prepared_job(broker)
    attempt, lease = dispatched(broker, job)
    _, lease = submit_receipt(broker, attempt, lease)
    before = Blob.objects.count()
    with pytest.raises(BrokerError) as error:
        services.report(broker.worker, attempt.id,
                        {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "observed",
                         "artifacts": {"undeclared.txt": base64.b64encode(b"no").decode()}})
    assert error.value.code == "INVALID_REPORT"
    assert Blob.objects.count() == before
    assert not Artifact.objects.exists()


def test_worker_and_public_requests_reject_oversized_inline_outputs(broker):
    budgeted(broker)
    job = prepared_job(broker, outputs=["large.bin"])
    attempt, lease = dispatched(broker, job)
    native, lease = submit_receipt(broker, attempt, lease)
    content = b"x" * (2 * 1024 * 1024)
    encoded = base64.b64encode(content).decode("ascii")
    document = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "observed",
                "native_identity": native, "artifacts": {"large.bin": encoded},
                "observation": {"state": "completed", "terminal": True,
                                "accounting": {"cpu_core_seconds": 2, "gpu_device_seconds": 0, "final": True}}}
    serialized = json.dumps(document)
    assert len(serialized.encode("utf-8")) > 2 * 1024 * 1024
    client = Client()
    response = client.post(f"/internal/v1/attempts/{attempt.id}/report", data=serialized,
                           content_type="application/json", HTTP_AUTHORIZATION="Bearer synthetic-worker-token")
    assert response.status_code == 413, response.content
    assert not Artifact.objects.filter(attempt=attempt).exists()
    broker.grant.refresh_from_db()
    assert (broker.grant.spent_cpu, broker.grant.reserved_cpu) == (0, 60)
    public = client.post(f"/api/v1/projects/{broker.project.id}/objects",
                         data=json.dumps({"path": "too-large.bin", "content_base64": encoded}),
                         content_type="application/json", HTTP_AUTHORIZATION="Bearer " + broker.token)
    assert public.status_code == 413


def test_file_backed_sqlite_serializes_parallel_budget_admission(tmp_path):
    """Independent DB connections contend for a real file, not in-memory SQLite."""
    script = r'''
import json, django
django.setup()
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from django.core.management import call_command
from django.db import close_old_connections, transaction
from django.utils import timezone
from compute_relay.control.models import *
from compute_relay.control.auth import token_hash
from compute_relay.control import services
from compute_relay.control.errors import BrokerError
call_command("migrate", run_syncdb=True, verbosity=0)
owner=User.objects.create_user(username="isolated", password=None)
worker=WorkerIdentity.objects.create(owner=owner,name="fake",token_hash=token_hash("synthetic-worker"))
connection=Connection.objects.create(owner=owner,worker=worker,name="fake",backend="fake",configuration={"site_profile":{"automatic_execution_allowed":True}},revision_sha256="c"*64)
environment=Environment.objects.create(owner=owner,connection=connection,name="fake Python",configuration={},revision_sha256="e"*64)
project=Project.objects.create(owner=owner,name="race")
project.connections.add(connection)
project.environments.add(environment)
credential=ProjectCredential.objects.create(project=project, issued_by=project.owner,name="agent",prefix="fake",token_hash=token_hash("synthetic-project"),scopes=["jobs:submit"],expires_at=timezone.now()+timedelta(days=1))
policy={"schema_version":"1.0","project_id":str(project.id),"revision":1,"mode":"budgeted","allowed_connection_ids":[str(connection.id)],"allowed_environment_revision_sha256":["e"*64],"allowed_gpu_types":[],"per_job_limits":{"cpu_cores":1,"gpu_devices":0,"memory_mib":128,"time_limit_seconds":60},"max_active_jobs":100,"allowance":{"cpu_core_seconds":180,"gpu_device_seconds":0},"admission_expires_at":(timezone.now()+timedelta(days=1)).isoformat()}
grant=Grant.objects.create(project=project,policy=policy)
job=Job.objects.create(project=project,credential=credential,request={"connection_id":str(connection.id),"environment":{"id":str(environment.id),"revision_sha256":"e"*64},"resources":{"cpus":1,"gpus":0,"memory_mib":128}},manifest={"connection_revision_sha256":"c"*64,"maximum_allocation":{"cpu_cores":1,"gpu_devices":0,"time_limit_seconds":60},"reservation":{"cpu_core_seconds":60,"gpu_device_seconds":0}},manifest_sha256="f"*64)
barrier=Barrier(6)
def reserve(index):
    close_old_connections()
    barrier.wait(timeout=15)
    try:
        with transaction.atomic():
            services.request_execution(Job.objects.get(id=job.id),ProjectCredential.objects.get(id=credential.id))
        return "authorized"
    except BrokerError as error:
        if error.code != "BUDGET_UNAVAILABLE": raise
        return "denied"
    finally:
        close_old_connections()
with ThreadPoolExecutor(max_workers=6) as executor:
    outcomes=list(executor.map(reserve,range(6)))
grant.refresh_from_db()
assert outcomes.count("authorized")==3 and outcomes.count("denied")==3, outcomes
assert grant.reserved_cpu==180 and Attempt.objects.count()==3
assert LedgerEntry.objects.filter(action="reserve").count()==3
print(json.dumps({"authorized":3,"reserved_cpu":grant.reserved_cpu}))
'''
    environment = dict(os.environ, DJANGO_SETTINGS_MODULE="compute_relay.control.settings",
                       PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
                       BROKER_DB_ENGINE="sqlite", BROKER_DATA_DIR=str(tmp_path / "service"), BROKER_DB_PATH=str(tmp_path / "race.sqlite3"),
                       BROKER_ARTIFACT_ROOT=str(tmp_path / "objects"), BROKER_PROFILE="local")
    result = subprocess.run([sys.executable, "-c", script], env=environment,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {"authorized": 3, "reserved_cpu": 180}
