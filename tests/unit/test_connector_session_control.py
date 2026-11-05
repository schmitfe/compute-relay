"""Owner policy and redacted session health survive existing connector pairings."""

from datetime import timedelta
import json

from django.test import Client
from django.utils import timezone
import pytest

from test_support.unit_test_connector_setup import personal


pytestmark = pytest.mark.django_db


def status(context, **changes):
    return Client().post("/internal/v1/connectors/tasks", json.dumps({
        "connection_id": str(context.connection.id), "status_only": True, **changes}),
        content_type="application/json", HTTP_AUTHORIZATION="Bearer " + context.token)


def test_owner_can_enable_and_revoke_existing_connector_without_changing_execution_scope(personal):
    registration = personal.connection.registration
    path = f"/clusters/{personal.connection.id}/connector-settings/"
    revision = personal.connection.revision_sha256
    for enabled in (True, False):
        response = personal.human.post(path, {"setup_revision": registration.revision,
            **({"automatic_reconnect_enabled": "on"} if enabled else {})})
        assert response.status_code == 302
        registration.refresh_from_db()
        assert registration.connector_settings["automatic_reconnect_enabled"] is enabled
        assert status(personal).json()["automatic_reconnect_enabled"] is enabled
    personal.connection.refresh_from_db()
    assert personal.connection.revision_sha256 == revision


def test_policy_change_requires_current_owner_revision_and_csrf(personal):
    path = f"/clusters/{personal.connection.id}/connector-settings/"
    assert personal.human.post(path, {"setup_revision": 999}).status_code == 409
    anonymous = Client()
    assert anonymous.post(path, {"automatic_reconnect_enabled": "on"},
        HTTP_AUTHORIZATION="Bearer " + personal.token).status_code == 403
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(personal.owner)
    assert csrf.post(path, {"setup_revision": personal.connection.registration.revision}).status_code == 403


def test_foreign_administrator_cannot_change_connector_permission(personal):
    from compute_relay.control.models import User
    user = User.objects.create_superuser(username="foreign-connector-admin", password=None)
    client = Client()
    client.force_login(user)
    response = client.post(f"/clusters/{personal.connection.id}/connector-settings/", {
        "setup_revision": personal.connection.registration.revision, "automatic_reconnect_enabled": "on"})
    assert response.status_code == 404
    assert status(personal).json()["automatic_reconnect_enabled"] is False


def test_status_only_never_leases_setup_work_and_saves_one_redacted_snapshot(personal):
    from compute_relay.control.models import ConnectorTask
    task = ConnectorTask.objects.create(connection=personal.connection, owner=personal.owner,
        kind="browse", request={"path": ""}, connection_revision=personal.connection.revision_sha256,
        expires_at=timezone.now() + timedelta(minutes=5))
    expires = (timezone.now() + timedelta(minutes=2)).isoformat()
    response = status(personal, runtime={"state": "connected", "expires_at": expires})
    assert response.status_code == 200
    assert response.json()["automatic_reconnect_enabled"] is False
    task.refresh_from_db()
    assert task.state == "queued" and task.lease_id is None
    registration = personal.connection.registration
    registration.refresh_from_db()
    assert registration.connector_settings["runtime"] == {
        "worker_id": str(personal.worker.id), "state": "connected", "expires_at": expires}
    page = personal.human.get(f"/ui/live/cluster/{personal.connection.id}/").json()["fragments"]["live-cluster-status"]
    assert "SSH session expires soon" in page
    assert status(personal, runtime={"state": "authentication_required", "expires_at": None}).status_code == 200
    page = personal.human.get(f"/ui/live/cluster/{personal.connection.id}/").json()["fragments"]["live-cluster-status"]
    assert "SSH sign-in required" in page and "Reconnect" in page


@pytest.mark.parametrize("runtime", [
    {"state": "remote exception", "expires_at": None},
    {"state": "connected", "expires_at": "not-a-date"},
    {"state": "connected", "expires_at": "2026-01-01T00:00:00"},
    {"state": "connected", "expires_at": None, "token": "private"},
    {"state": [], "expires_at": None},
])
def test_runtime_boundary_rejects_unbounded_or_untyped_status(personal, runtime):
    assert status(personal, runtime=runtime).status_code == 400


def test_worker_cannot_write_owner_permission(personal):
    response = status(personal, automatic_reconnect_enabled=True)
    assert response.status_code == 400
    assert status(personal).json()["automatic_reconnect_enabled"] is False
