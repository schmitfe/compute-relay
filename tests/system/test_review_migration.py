"""Upgrade a populated v1 database without inferring input authority."""

import subprocess
import sys

from test_support.system_test_local_application import environment


def test_populated_v1_migration_preserves_accounting_and_seals_only_evidenced_sources(tmp_path):
    script = '''
import os, sys
from datetime import timedelta
from uuid import uuid4
from compute_relay.local_state import LocalState
from compute_relay.local_runtime import service_environment
owner_id = str(uuid4())
with LocalState(sys.argv[1]) as state:
    document = {"schema_version":"1.0", "installation_id":str(uuid4()), "owner_id":owner_id,
        "portal_auth":"none", "port":8768, "extensions":["environments","workflows"], "initialized":True}
    state.write("installation.json", document)
    os.environ.update(service_environment(state, document, str(uuid4())))
os.environ["DJANGO_SETTINGS_MODULE"] = "compute_relay.control.settings"
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
old_targets = [("broker","0001_initial"), ("broker_environments","0001_initial"), ("broker_workflows","0001_initial")]
executor = MigrationExecutor(connection)
executor.migrate(old_targets)
old = executor.loader.project_state(old_targets).apps
model = lambda name: old.get_model("broker", name)
owner = model("User").objects.create(id=owner_id, username="local", password="!")
worker = model("WorkerIdentity").objects.create(owner=owner, name="fixture", token_hash="1" * 64)
cluster = model("Connection").objects.create(owner=owner, worker=worker, name="fixture", backend="fake",
    configuration={}, revision_sha256="2" * 64)
project = model("Project").objects.create(owner=owner, name="migration fixture", storage_reserved=5)
credential = model("ProjectCredential").objects.create(project=project, issued_by=owner, name="fixture",
    token_hash="3" * 64, prefix="fixture", expires_at=timezone.now() + timedelta(days=1))
env = model("Environment").objects.create(owner=owner, connection=cluster, name="fixture",
    configuration={}, revision_sha256="4" * 64)
grant = model("Grant").objects.create(project=project, policy={}, reserved_cpu=60)
manifest = {"adapter_version":"slurm-ssh/v1", "helper_sha256":"5" * 64}
job = model("Job").objects.create(project=project, credential=credential, state="prepared",
    request={"name":"existing job", "connection_id":str(cluster.id)}, manifest=manifest, manifest_sha256="6" * 64)
execution = model("ExecutionRequest").objects.create(job=job, credential=credential, state="authorized")
native = {"cluster":"fixture", "job_id":"42", "attempt_id":str(uuid4())}
attempt = model("Attempt").objects.create(job=job, execution_request=execution, connection=cluster, grant=grant,
    state="running", dispatch_started=True, native_identity=native, reservation_cpu=60, reservation_gpu=0)
model("LedgerEntry").objects.create(grant=grant, attempt=attempt, action="reserve", cpu=60, gpu=0)
raw = model("Blob").objects.create(project=project, sha256="7" * 64, size_bytes=10)
output = model("Blob").objects.create(project=project, sha256="8" * 64, size_bytes=12)
model("JobFile").objects.create(job=job, path="source.py", blob=raw)
model("JobFile").objects.create(job=job, path="prior-output.json", blob=output)
artifact = model("Artifact").objects.create(attempt=attempt, path="prior-output.json", blob=output)
model("UploadReservation").objects.create(project=project, size_bytes=5)
model("StoragePool").objects.create(id=1, reserved=5)
model("IdempotencyRecord").objects.create(project=project, operation="execute", key="original-request",
    request_sha256="9" * 64, response={"attempt_id":str(attempt.id)}, status=202)
latest_receipt = None
for generation in range(20):
    latest_receipt = model("WorkerReport").objects.create(attempt=attempt, generation=generation,
        lease_id=uuid4(), payload_sha256="c" * 64, response={"accepted":True})
spec = old.get_model("broker_environments","EnvironmentSpec").objects.create(project=project,
    connection=cluster, base_environment=env, document={"requirements":[]}, sha256="a" * 64)
workflow = old.get_model("broker_workflows","WorkflowRevision").objects.create(project=project,
    name="fixture", document={"nodes":[]}, sha256="b" * 64)
executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
new = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps
model = lambda name: new.get_model("broker", name)
assert model("User").objects.count() == 1
current = model("Attempt").objects.get(id=attempt.id)
assert current.state == "running" and current.dispatch_started and current.native_identity == native
assert current.accounting_state == "reserved" and current.lease_deadline_at is None
assert model("Grant").objects.get(id=grant.id).reserved_cpu == 60
assert model("LedgerEntry").objects.filter(attempt_id=attempt.id).count() == 1
assert model("Job").objects.get(id=job.id).manifest == manifest
assert model("JobFile").objects.get(path="source.py").source_kind == "unverified"
sealed = model("JobFile").objects.get(path="prior-output.json")
assert sealed.source_kind == "artifact" and sealed.source_artifact_id == artifact.id
assert model("UploadGrant").objects.count() == 0 and model("InputSource").objects.count() == 0
reservation = model("UploadReservation").objects.get()
assert reservation.state == "reserved" and reservation.sha256 == "" and reservation.expires_at is None
assert model("Project").objects.get(id=project.id).storage_reserved == 5
receipt = model("IdempotencyRecord").objects.get()
assert receipt.key == "original-request" and not receipt.compacted and receipt.response["attempt_id"] == str(attempt.id)
assert model("WorkerReport").objects.filter(attempt_id=attempt.id).count() == 16
assert model("WorkerReport").objects.filter(lease_id=latest_receipt.lease_id).exists()
charges = model("MetadataUsage").objects
assert charges.filter(kind="job", object_id=str(job.id), size_bytes__gt=32768).exists()
assert charges.filter(kind="idempotency", size_bytes__gt=512).count() == 1
assert charges.filter(kind="environment_spec", object_id=str(spec.id)).exists()
assert charges.filter(kind="workflow_revision", object_id=str(workflow.id)).exists()
print("populated v1 migration preserved authority and accounting")
'''
    result = subprocess.run([sys.executable, "-I", "-c", script, str(tmp_path / "state")],
        cwd=tmp_path, env=environment(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "preserved authority and accounting" in result.stdout
