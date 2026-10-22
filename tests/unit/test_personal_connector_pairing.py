import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from compute_broker.control.auth import token_hash
from compute_broker.control.models import ClusterRegistration, ConnectorEnrollment, Connection, User, WorkerIdentity, AuditEvent
from compute_broker.worker.enrollment import profile_metadata

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup_cluster():
    owner = User.objects.create_user(username="personal-owner")
    registration = ClusterRegistration.objects.create(owner=owner, name="My HPC", host="cluster.example", remote_user="hpc-user",
        requested_root="/scratch/user/broker", connector_settings={"identity_file": "~/.ssh/lab-key", "session_seconds": 3600})
    human = Client();human.force_login(owner)
    return owner, registration, human


def download(context):
    _, registration, human = context
    registration.refresh_from_db()
    response = human.post(f"/clusters/setup/{registration.id}/connect/", {"setup_revision": registration.revision})
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    return response.json()


def public_profile(registration):
    return profile_metadata("personal-test", {"backend": "slurm", "allowed_hosts": [registration.host],
        "allowed_users": [registration.remote_user], "allowed_ports": [registration.port],
        "remote_root_prefixes": [registration.requested_root], "site_profile": {"automatic_execution_allowed": False},
        "ssh": {"mode": "mfa", "control_path": "/private/local/s", "known_hosts": "/private/local/known_hosts"}})


def enroll(context, document, profile=None):
    return Client().post("/internal/v1/connectors/enroll", json.dumps({"profile": profile or public_profile(context[1])}),
        content_type="application/json", HTTP_AUTHORIZATION="Bearer " + document["pairing_token"])


def test_pairing_automatically_creates_owned_worker_and_discovery_binding(setup_cluster):
    owner, registration, _ = setup_cluster
    document = download(setup_cluster)
    enrollment = ConnectorEnrollment.objects.get()
    assert enrollment.token_hash == token_hash(document["pairing_token"])
    assert WorkerIdentity.objects.count() == 0
    response = enroll(setup_cluster, document)
    assert response.status_code == 201
    result = response.json()
    worker = WorkerIdentity.objects.get()
    connection = Connection.objects.get()
    assert worker.owner == owner and worker.token_hash == token_hash(result["worker_token"])
    assert connection.owner == owner and connection.worker == worker
    assert connection.discovery_state == "queued" and not connection.setup_complete
    serialized = json.dumps(connection.configuration) + json.dumps(worker.profiles) + json.dumps(list(AuditEvent.objects.values()), default=str)
    assert document["pairing_token"] not in serialized and result["worker_token"] not in serialized
    assert "/private/local" not in serialized and "lab-key" not in serialized
    assert enroll(setup_cluster, document).status_code == 409
    assert WorkerIdentity.objects.count() == 1


@pytest.mark.parametrize("change", ["expired", "stale", "disabled", "reissued"])
def test_pairing_rechecks_current_human_authority_and_setup(setup_cluster, change):
    owner, registration, _ = setup_cluster
    document = download(setup_cluster)
    if change == "expired":
        ConnectorEnrollment.objects.update(expires_at=timezone.now()-timedelta(seconds=1))
    elif change == "stale":
        registration.revision += 1;registration.save()
    elif change == "disabled":
        owner.is_active = False;owner.save()
    else:
        download(setup_cluster)
    assert enroll(setup_cluster, document).status_code in {403, 409}
    assert not WorkerIdentity.objects.exists() and not Connection.objects.exists()


@pytest.mark.parametrize("field,value", [("allowed_hosts", ["other.example"]), ("allowed_users", ["root"]),
    ("remote_root_prefixes", ["/"]), ("site_profile", {"automatic_execution_allowed": True}), ("revision_sha256", "bad")])
def test_pairing_cannot_widen_scope_or_partially_create_worker(setup_cluster, field, value):
    document = download(setup_cluster)
    profile = public_profile(setup_cluster[1]);profile[field] = value
    assert enroll(setup_cluster, document, profile).status_code in {400, 403}
    assert not WorkerIdentity.objects.exists() and not ConnectorEnrollment.objects.get().consumed_at


def test_repair_preserves_connection_and_revokes_only_previous_personal_worker(setup_cluster):
    first = enroll(setup_cluster, download(setup_cluster)).json()
    second = enroll(setup_cluster, download(setup_cluster)).json()
    assert first["connection_id"] == second["connection_id"] and first["worker_id"] != second["worker_id"]
    assert WorkerIdentity.objects.get(pk=first["worker_id"]).revoked
    assert not WorkerIdentity.objects.get(pk=second["worker_id"]).revoked


def test_only_own_human_session_with_csrf_can_download(setup_cluster):
    owner, registration, human = setup_cluster
    path = f"/clusters/setup/{registration.id}/connect/"
    other = User.objects.create_superuser(username="other-admin", password="synthetic")
    foreign = Client();foreign.force_login(other)
    assert foreign.post(path, {"setup_revision": 1}).status_code == 404
    assert Client().post(path, {"setup_revision": 1}, HTTP_AUTHORIZATION="Bearer project-token").status_code == 403
    csrf = Client(enforce_csrf_checks=True);csrf.force_login(owner)
    assert csrf.post(path, {"setup_revision": 1}).status_code == 403
    assert human.get(path).status_code == 405
