from datetime import timedelta
import json
from types import SimpleNamespace
import pytest
from django.test import Client
from django.utils import timezone
from compute_relay.control.auth import token_hash
from compute_relay.control.models import Connection, ConnectorEnrollment, ClusterRegistration, User, WorkerIdentity
from compute_relay.control.services import digest
from compute_relay.worker.enrollment import profile_metadata, resolve_connection

@pytest.fixture
def personal():
    owner = User.objects.create_user(username="personal-setup-owner")
    token = "cbw_synthetic-connector-token"
    site = {"partitions": ["cpu"], "accounts": [], "qos": [], "maximum_cpus": 8,
        "maximum_gpus": 0, "maximum_memory_mib": 8192, "maximum_time_limit_seconds": 600,
        "automatic_execution_allowed": False}
    protected = {"backend": "slurm", "allowed_hosts": ["cluster.example"], "allowed_users": ["researcher"],
        "allowed_ports": [22], "remote_root_prefixes": ["/home/researcher/jobs"], "site_profile": site,
        "profile_revision": 1, "ssh": {"mode": "mfa", "control_path": "/private/s", "known_hosts": "/private/known_hosts"}}
    metadata = profile_metadata("personal-test", protected)
    worker = WorkerIdentity.objects.create(owner=owner, name="Personal", token_hash=token_hash(token),
        profiles=[metadata], setup_ready=True)
    public = resolve_connection({"backend": "slurm", "credential_profile": "personal-test",
        "ssh": {"host": "cluster.example", "user": "researcher", "port": 22}, "remote_root": "/home/researcher/jobs"},
        {"personal-test": protected})[1]
    snapshot = {"nodes": [{"name": "n1", "partition": "cpu", "cpus": "8", "memory_mib": "8192", "gres": "(null)"}],
        "partitions": [{"PartitionName": "cpu", "MaxTime": "00:10:00"}], "associations": []}
    connection = Connection.objects.create(owner=owner, worker=worker, name="Cluster", backend="slurm",
        configuration=public, revision_sha256=digest(public), discovery_snapshot=snapshot, discovery_state="ready")
    registration = ClusterRegistration.objects.create(owner=owner, name="Cluster", host="cluster.example", remote_user="researcher",
        requested_root="/home/researcher/jobs", connection=connection)
    ConnectorEnrollment.objects.create(registration=registration, setup_revision=1, worker=worker,
        token_hash=token_hash("cbpair_synthetic"), consumed_at=timezone.now(), expires_at=timezone.now() + timedelta(minutes=5))
    human = Client();human.force_login(owner)
    return SimpleNamespace(owner=owner, worker=worker, connection=connection, registration=registration, human=human,
        token=token, protected=protected, site=site)

def api(context, path, payload):
    return Client().post(path, json.dumps(payload), content_type="application/json", HTTP_AUTHORIZATION="Bearer " + context.token)

def lease(context):
    response = api(context, "/internal/v1/connectors/tasks", {"connection_id": str(context.connection.id)})
    assert response.status_code == 200
    return response.json()["task"]

def reply(context, task, **changes):
    return api(context, f"/internal/v1/connectors/tasks/{task['id']}/report", {
        "connection_id": str(context.connection.id), "lease_id": task["lease_id"], "generation": task["generation"],
        "outcome": "succeeded", "result": {"ok": True, "path": "/home/researcher", "parent": "/home", "home": "/home/researcher",
            "entries": [{"name": "science", "path": "/home/researcher/science"}], "writable": True, "truncated": False,
            "scanned_entries": 1}, **changes})

def configuration_fields(context, **changes):
    result = {key: ", ".join(value) if isinstance(value, list) else value for key, value in context.site.items()
        if key != "automatic_execution_allowed"}
    result["maximum_wall_minutes"] = result.pop("maximum_time_limit_seconds") // 60
    return {**result, "remote_root": "/scratch/researcher/jobs", "connection_revision": context.connection.revision_sha256, **changes}
