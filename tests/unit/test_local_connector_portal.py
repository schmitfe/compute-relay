"""Only a loopback human portal action can queue local SSH authentication."""

import json
from pathlib import Path
import tempfile
from uuid import uuid4

from django.test import Client
from django.urls import path
import pytest

from compute_relay.control import local_connectors as portal
from compute_relay.control.auth import token_hash
from compute_relay.control.models import ClusterRegistration, ConnectorEnrollment, User
from compute_relay.local_connectors import LocalConnectorRequests, connector_directory


urlpatterns = [path("start/<uuid:registration_id>/", portal.start)]
ORIGIN = "http://127.0.0.1:8768"


@pytest.fixture
def local_cluster(db, settings):
    with tempfile.TemporaryDirectory(prefix="cb-portal-") as value:
        settings.DATA_DIR = Path(value)
        settings.BROKER_LOCAL_RUNTIME = "1"
        settings.BROKER_LOCAL_ORIGIN = ORIGIN
        settings.BROKER_LOCAL_AUTH_MODE = "password"
        settings.BROKER_LOCAL_OWNER_ID = None
        settings.ROOT_URLCONF = __name__
        settings.ALLOWED_HOSTS = ["127.0.0.1", "evil.example"]
        owner = User.objects.create_user(username="local-connector-owner")
        registration = ClusterRegistration.objects.create(owner=owner, name="Lab <cluster>", host="cluster.example.invalid",
            remote_user="scientist", connector_settings={"identity_file": "~/.ssh/lab", "session_seconds": 3600})
        human = Client()
        human.force_login(owner)
        yield owner, registration, human, settings.DATA_DIR


def post(context, *, client=None, extra=None, **headers):
    _, registration, human, _ = context
    body = {"setup_revision": str(registration.revision), **(extra or {})}
    return (client or human).post(f"/start/{registration.id}/", body,
        **{"HTTP_HOST": "127.0.0.1:8768", "REMOTE_ADDR": "127.0.0.1", **headers})


def test_human_start_queues_private_setup_without_exposing_token(local_cluster):
    owner, registration, _, data = local_cluster
    response = post(local_cluster)
    assert response.status_code == 202
    assert "no-store" in response["Cache-Control"]
    document = LocalConnectorRequests(data).take(ORIGIN)
    assert document["registration_id"] == str(registration.id)
    assert document["setup"]["owner_id"] == str(owner.id)
    assert document["setup"]["broker_url"] == ORIGIN
    assert ConnectorEnrollment.objects.get().token_hash == token_hash(document["setup"]["pairing_token"])
    assert document["setup"]["pairing_token"].encode() not in response.content
    assert b"Lab &lt;cluster&gt;" in response.content


def test_duplicate_start_does_not_expire_or_reissue_pending_pairing(local_cluster):
    assert post(local_cluster).status_code == 202
    enrollment = ConnectorEnrollment.objects.get()
    assert post(local_cluster).status_code == 409
    assert ConnectorEnrollment.objects.count() == 1
    enrollment.refresh_from_db()
    assert enrollment.expires_at > enrollment.created_at


@pytest.mark.parametrize("headers", [{"HTTP_AUTHORIZATION": "Bearer synthetic-project-key"},
    {"HTTP_HOST": "evil.example"}, {"REMOTE_ADDR": "192.0.2.1"}])
def test_bearer_and_nonlocal_origins_cannot_queue_even_with_human_cookie(local_cluster, headers):
    response = post(local_cluster, **headers)
    assert response.status_code == 403
    assert not ConnectorEnrollment.objects.exists()
    assert not (local_cluster[3] / "connector-requests").exists()


def test_agent_key_without_human_cookie_never_queues(local_cluster):
    response = post(local_cluster, client=Client(), HTTP_AUTHORIZATION="Bearer synthetic-project-key")
    assert response.status_code == 403
    assert not ConnectorEnrollment.objects.exists()


def test_csrf_is_required_for_human_start(local_cluster):
    owner, _, _, _ = local_cluster
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    assert post(local_cluster, client=client).status_code == 403
    assert not ConnectorEnrollment.objects.exists()


@pytest.mark.parametrize("flag", ["", "0", False])
def test_ordinary_server_cannot_queue_local_start(local_cluster, settings, flag):
    settings.BROKER_LOCAL_RUNTIME = flag
    assert post(local_cluster).status_code == 403
    assert not ConnectorEnrollment.objects.exists()


@pytest.mark.parametrize("extra", [{"state_directory": "/tmp/other"}, {"host": "evil.example"}, {"setup_revision": "999"}])
def test_browser_cannot_supply_paths_targets_or_stale_revision(local_cluster, extra):
    assert post(local_cluster, extra=extra).status_code in {400, 409}
    assert not ConnectorEnrollment.objects.exists()


def test_existing_private_state_resumes_without_reading_or_issuing_pairing(local_cluster):
    _, registration, _, data = local_cluster
    directory = connector_directory(data, registration.id)
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    # Invalid JSON proves that the portal validates metadata without reading a
    # worker token or SSH identity configuration from state.
    (directory / "state.json").write_text("synthetic-private-state")
    (directory / "state.json").chmod(0o600)
    assert post(local_cluster).status_code == 202
    assert not ConnectorEnrollment.objects.exists()
    document = LocalConnectorRequests(data).take(ORIGIN)
    assert document["setup"] is None


def test_unsafe_saved_state_is_not_repaired_or_read_implicitly(local_cluster):
    _, registration, _, data = local_cluster
    directory = connector_directory(data, registration.id)
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    other = data / "other"
    other.write_text("synthetic-other-private-state")
    (directory / "state.json").symlink_to(other)
    assert post(local_cluster).status_code == 409
    assert other.read_text() == "synthetic-other-private-state"
    assert not ConnectorEnrollment.objects.exists()


def test_foreign_user_cannot_queue_owner_cluster(local_cluster):
    foreign = User.objects.create_user(username="different-local-user")
    client = Client()
    client.force_login(foreign)
    assert post(local_cluster, client=client).status_code == 404
    assert not ConnectorEnrollment.objects.exists()


def test_local_start_is_post_only(local_cluster):
    _, registration, human, _ = local_cluster
    assert human.get(f"/start/{registration.id}/", HTTP_HOST="127.0.0.1:8768").status_code == 405
