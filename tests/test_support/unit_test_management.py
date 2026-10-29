from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from django.test import Client
from django.utils import timezone
from compute_relay.control import services
from compute_relay.control.auth import new_token, token_hash
from compute_relay.control.models import Attempt, Connection, Environment, ExecutionRequest, Grant, Job, Project, ProjectCredential, User, WorkerIdentity
from compute_relay.control.storage import store_uploaded_blob

SCOPES = ["objects:write", "jobs:read", "jobs:write", "jobs:execute", "jobs:cancel", "artifacts:read"]

@pytest.fixture
def management(db, settings, tmp_path):
    settings.ARTIFACT_ROOT = tmp_path / "objects"
    owner = User.objects.create_user(username="management-owner", password=None)
    outsider = User.objects.create_user(username="management-outsider", password=None)
    worker = WorkerIdentity.objects.create(name="management-worker", owner=owner,
                                           token_hash=token_hash("synthetic-management-worker"),
                                           profiles=[{"alias": "synthetic-profile", "backend": "fake",
                                                      "allowed_hosts": ["fixture.invalid"], "allowed_users": ["fixtureuser"],
                                                      "allowed_ports": [22], "remote_root_prefixes": ["/fixture/jobs"]}])
    profile = {"partitions": ["debug"], "accounts": ["broker"], "qos": [],
               "maximum_cpus": 2, "maximum_gpus": 1, "maximum_memory_mib": 1024,
               "maximum_time_limit_seconds": 600, "automatic_execution_allowed": True}
    worker.profiles[0].update({"revision_sha256": "a" * 64, "site_profile": profile})
    worker.save(update_fields=["profiles"])
    connections = []
    for index in range(2):
        config = {"backend": "fake", "profile_revision": 1, "site_profile": profile,
                  "state_directory": str(tmp_path / f"fake-{index}")}
        connections.append(Connection.objects.create(owner=owner, worker=worker, name=f"owned-{index}",
                                                     backend="fake", configuration=config,
                                                     discovery_snapshot={"node_inventory_complete": True,
                                                         "nodes": [{"partition": "debug", "gres": "gpu:a100:1"}]},
                                                     revision_sha256=services.digest(config)))
    foreign = Connection.objects.create(owner=outsider, worker=worker, name="private-outsider-cluster",
                                         backend="fake", configuration={}, revision_sha256="f" * 64)
    environment = Environment.objects.create(owner=owner, connection=connections[0], name="management Python", configuration={"interpreter": "/usr/bin/python3", "modules": []},
                                              revision_sha256="e" * 64)
    secondary_environment = Environment.objects.create(owner=owner, connection=connections[1], name="second cluster Python",
        configuration={"interpreter": "/usr/bin/python3", "modules": []}, revision_sha256="e" * 64)
    project = Project.objects.create(name="managed project", owner=owner)
    project.connections.set(connections)
    project.environments.add(environment, secondary_environment)
    policy = {"schema_version": "1.0", "project_id": str(project.id), "revision": 1,
              "mode": "approval_required", "allowed_connection_ids": [str(c.id) for c in connections],
              "allowed_environment_revision_sha256": [environment.revision_sha256],
              "allowed_gpu_types": ["a100"], "per_job_limits": {
                  "cpu_cores": 2, "gpu_devices": 1, "memory_mib": 512,
                  "time_limit_seconds": 600},
              "max_active_jobs": 2, "allowance": {"cpu_core_seconds": 7200, "gpu_device_seconds": 3600},
              "admission_expires_at": (timezone.now() + timedelta(days=3)).isoformat()}
    grant = Grant.objects.create(project=project, policy=policy)
    token = new_token()
    credential = ProjectCredential.objects.create(project=project, issued_by=project.owner, name="original agent", prefix=token[:12],
                                                   token_hash=token_hash(token), scopes=SCOPES,
                                                   expires_at=timezone.now() + timedelta(days=1))
    return SimpleNamespace(owner=owner, outsider=outsider, worker=worker, connections=connections,
                           foreign=foreign, environment=environment, secondary_environment=secondary_environment, project=project, grant=grant,
                           credential=credential, token=token)

def human(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    client.get("/login/")
    assert "csrftoken" in client.cookies
    return client

def form_post(client, path, data=None):
    return client.post(path, data or {}, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)

def key_data(context, **changes):
    data = {"name": "separate agent", "expires_at": (timezone.now() + timedelta(days=2)).isoformat(),
            "scopes": SCOPES, "cluster_access": "inherit", "allowed_connections": [], "execution_mode": "inherit", "submission_id": str(uuid4())}
    data.update(changes)
    return data

def issued(response):
    assert response.status_code in {200, 201}
    secret = response.context["secret"]
    credential = response.context["credential"]
    assert secret.startswith("cbp_") and len(secret) >= 40
    assert "no-store" in response.headers.get("Cache-Control", "").lower()
    assert credential.token_hash == token_hash(secret)
    assert credential.prefix != secret[:len(credential.prefix)]
    return secret, credential

def agent_get(token, path):
    return Client().get(path, HTTP_AUTHORIZATION="Bearer " + token)

def prepared(context, connection=None, outputs=None, *, name="management-test"):
    source = b"raise RuntimeError('management tests must never execute uploaded source')\n"
    blob = store_uploaded_blob(context.project, source)
    connection = connection or context.connections[0]
    environment = context.secondary_environment if connection.id == context.connections[1].id else context.environment
    document = {"schema_version": "1.0", "project_id": str(context.project.id), "connection_id": str(connection.id),
                "name": name, "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
                "environment": {"id": str(environment.id), "revision_sha256": environment.revision_sha256},
                "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
                "slurm": {"partition": "debug", "account": "broker"},
                "files": [{"path": "main.py", "sha256": blob.sha256, "size_bytes": blob.size_bytes, "executable": False}],
                "outputs": outputs or []}
    created, _ = services.create_job(context.credential, document)
    job = Job.objects.get(id=created["id"])
    services.prepare_job(job)
    job.refresh_from_db()
    return job

def queued_discovery(context):
    config = {"backend": "fake", "credential_profile": "synthetic-profile",
              "ssh": {"host": "fixture.invalid", "user": "fixtureuser", "port": 22},
              "remote_root": "/fixture/jobs/project"}
    return Connection.objects.create(owner=context.owner, worker=context.worker, name="discovery fixture", backend="fake",
                                     configuration=config, revision_sha256=services.digest(config), discovery_state="queued")

def discovery_report(connection, worker, lease, **changes):
    config = {**connection.configuration, "profile_revision": 1,
              "site_profile": worker.profiles[0]["site_profile"],
              "credential_profile_revision_sha256": worker.profiles[0]["revision_sha256"]}
    report = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "discovered",
              "configuration": config, "connection_revision_sha256": services.digest(config),
              "discovery_snapshot": {"scope": "synthetic", "partitions": [{"name": "debug"}],
                                     "software": {"scope": "login_node", "verified_on_compute": False}}}
    report.update(changes)
    return report

def outstanding_discovery_attempt(context, connection):
    job = Job.objects.create(project=context.project, credential=context.credential,
                             request={"name": "unsettled-discovery-safety-job"})
    request = ExecutionRequest.objects.create(job=job, credential=context.credential, state="approved")
    return Attempt.objects.create(job=job, execution_request=request, connection=connection, grant=context.grant,
                                  state="succeeded", accounting_state="settlement_pending", reservation_cpu=95, reservation_gpu=0)
