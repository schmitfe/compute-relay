from datetime import timedelta
from types import SimpleNamespace
import pytest
from django.db import transaction
from django.utils import timezone
from compute_relay.control import services
from compute_relay.control.auth import token_hash
from compute_relay.control.models import Attempt, Connection, Environment, ExecutionRequest, Grant, Job, Project, ProjectCredential, User, WorkerIdentity
from compute_relay.control.storage import store_uploaded_blob

@pytest.fixture
def broker(db, settings, tmp_path):
    settings.ARTIFACT_ROOT = tmp_path / "objects"
    owner = User.objects.create_user(username="owner", password=None)
    outsider = User.objects.create_user(username="outsider", password=None)
    worker = WorkerIdentity.objects.create(owner=owner, name="synthetic-worker", token_hash=token_hash("synthetic-worker-token"))
    connection = Connection.objects.create(owner=owner, worker=worker, name="fake", backend="fake",
                                           configuration={"state_directory": str(tmp_path / "fake"),
                                               "site_profile": {"automatic_execution_allowed": True}},
                                           revision_sha256="c" * 64)
    environment = Environment.objects.create(name="python", owner=owner, connection=connection,
                                              configuration={"interpreter": "/usr/bin/python3"},
                                              revision_sha256="e" * 64)
    project = Project.objects.create(name="synthetic project", owner=owner)
    project.connections.add(connection)
    project.environments.add(environment)
    token = "synthetic-project-token"
    credential = ProjectCredential.objects.create(project=project, issued_by=project.owner, name="agent", prefix="synthetic",
                                                   token_hash=token_hash(token),
                                                   scopes=["jobs:write", "jobs:execute", "jobs:read", "jobs:cancel", "artifacts:read", "objects:write"],
                                                   expires_at=timezone.now() + timedelta(days=1))
    policy = {"schema_version": "1.0", "project_id": str(project.id), "revision": 1,
              "mode": "approval_required", "allowed_connection_ids": [str(connection.id)],
              "allowed_environment_revision_sha256": [environment.revision_sha256],
              "allowed_gpu_types": ["a100"], "per_job_limits": {
                  "cpu_cores": 8, "gpu_devices": 1, "memory_mib": 512,
                  "time_limit_seconds": 3600},
              "max_active_jobs": 100,
              "allowance": {"cpu_core_seconds": 10000, "gpu_device_seconds": 10000},
              "admission_expires_at": (timezone.now() + timedelta(days=1)).isoformat()}
    grant = Grant.objects.create(project=project, policy=policy)
    return SimpleNamespace(owner=owner, outsider=outsider, worker=worker, connection=connection,
                           environment=environment, project=project, credential=credential,
                           token=token, grant=grant, policy=policy)

def prepared_job(broker, *, cpus=1, outputs=None):
    source = b"raise RuntimeError('uploaded source must not execute in the broker')\n"
    blob = store_uploaded_blob(broker.project, source)
    request = {"schema_version": "1.0", "project_id": str(broker.project.id),
               "connection_id": str(broker.connection.id), "name": "synthetic",
               "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
               "environment": {"id": str(broker.environment.id), "revision_sha256": broker.environment.revision_sha256},
               "resources": {"nodes": 1, "cpus": cpus, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
               "slurm": {"partition": "debug", "account": "broker"},
               "files": [{"path": "main.py", "sha256": blob.sha256, "size_bytes": blob.size_bytes, "executable": False}],
               "outputs": outputs or []}
    with transaction.atomic():
        result, _ = services.create_job(broker.credential, request)
    job = Job.objects.get(id=result["id"])
    services.prepare_job(job)
    job.refresh_from_db()
    return job

def execution(broker, job, credential=None):
    with transaction.atomic():
        result, _ = services.request_execution(job, credential or broker.credential)
    return ExecutionRequest.objects.get(id=result["id"])

def decide(broker, job, request, approved=True, **changes):
    document = {"execution_request_id": str(request.id), "manifest_sha256": job.manifest_sha256,
                "approved": approved, **changes}
    with transaction.atomic():
        return services.decide(job, broker.owner, document)

def budgeted(broker, cpu=10000, gpu=10000):
    broker.grant.policy = {**broker.grant.policy, "mode": "budgeted",
                           "allowance": {"cpu_core_seconds": cpu, "gpu_device_seconds": gpu}}
    broker.grant.save(update_fields=["policy"])

def dispatched(broker, job):
    request = execution(broker, job)
    attempt = request.attempt
    lease = services.lease_work(broker.worker)["lease"]
    dispatch = {"lease_id": lease["id"], "generation": lease["generation"],
                "manifest_sha256": job.manifest_sha256,
                "connection_revision_sha256": broker.connection.revision_sha256}
    services.dispatch(broker.worker, attempt.id, dispatch)
    return attempt, lease

def submit_receipt(broker, attempt, lease):
    native = {"job_id": "17", "attempt_id": str(attempt.id), "cluster": "simulation"}
    services.report(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
                                               "outcome": "submitted", "native_identity": native})
    Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now() - timedelta(seconds=1))
    next_lease = services.lease_work(broker.worker)["lease"]
    assert next_lease["operation"] == "observe"
    return native, next_lease


def publish_output(lease, path, content):
    """Publish synthetic bytes through the ordinary fenced worker transfer API."""
    import base64
    import hashlib
    import json
    from django.test import Client
    from compute_relay.transfer import CHUNK_BYTES
    client = Client(HTTP_AUTHORIZATION="Bearer synthetic-worker-token")
    endpoint = f"/internal/v1/attempts/{lease['attempt_id']}/output-chunk"
    common = {"lease_id": lease["id"], "generation": lease["generation"], "path": path}
    def send(body):
        response = client.post(endpoint, data=json.dumps({**common, **body}), content_type="application/json")
        assert response.status_code == 200, response.content
        return response.json()
    reply = send({"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)})
    while not reply["complete"]:
        offset = reply["offset"]
        chunk = content[offset:offset + CHUNK_BYTES]
        reply = send({"transfer_id": reply["transfer_id"], "offset": offset,
            "content_base64": base64.b64encode(chunk).decode("ascii"), "finish": offset + len(chunk) == len(content)})
    return reply
