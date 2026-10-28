from datetime import timedelta
import json
from uuid import uuid4
import pytest
from django.test import Client
from django.utils import timezone
from compute_relay.control.models import ClusterRegistration, ConnectorEnrollment
from compute_relay.control.auth import token_hash
from compute_relay.extensions.storage.models import StorageTargetRegistration

@pytest.fixture
def personal(resources):
    resources.profile["storage_roots"] = []
    resources.worker.profiles = [resources.profile]
    resources.worker.connector_features = ["personal_setup_v1", "personal_folders_v1"]
    resources.worker.save(update_fields=["profiles", "connector_features"])
    registration = ClusterRegistration.objects.create(owner=resources.owner, name="Personal storage cluster",
        host="storage.invalid", remote_user="researcher", connection=resources.connection)
    ConnectorEnrollment.objects.create(registration=registration, worker=resources.worker, setup_revision=1,
        token_hash=token_hash("pairing-storage"), consumed_at=timezone.now(),
        expires_at=timezone.now() + timedelta(minutes=5))
    resources.human = Client()
    resources.human.force_login(resources.owner)
    return resources

def document(personal, **changes):
    return {"connection": str(personal.connection.id), "connection_revision": personal.connection.revision_sha256,
        "name": "Project results", "root_path": "/scratch/researcher/results", "roles": ["work", "results"],
        "submission_id": str(uuid4()), **changes}

def target_path(personal, suffix=""):
    return f"/projects/{personal.project.id}/storage/targets/" + suffix

def queue(personal, **changes):
    response = personal.human.post(target_path(personal, "new/"), document(personal, **changes))
    assert response.status_code == 302, response.content
    return StorageTargetRegistration.objects.latest("created_at")

def worker_post(personal, path, payload):
    return Client().post(path, json.dumps(payload), content_type="application/json",
        HTTP_AUTHORIZATION="Bearer storage-worker")

def complete(personal, registration, **changes):
    response = worker_post(personal, "/internal/v1/connectors/tasks", {"connection_id": str(personal.connection.id)})
    assert response.status_code == 200, response.content
    task = response.json()["task"]
    assert task["id"] == str(registration.task_id)
    result = {"ok": True, "path": registration.root, "parent": "/scratch/researcher", "home": "/home/researcher",
        "entries": [], "writable": True, "truncated": False, "scanned_entries": 0, "created": True}
    return worker_post(personal, f"/internal/v1/connectors/tasks/{task['id']}/report", {
        "connection_id": str(personal.connection.id), "lease_id": task["lease_id"], "generation": task["generation"],
        "outcome": "succeeded", "result": result, **changes})
