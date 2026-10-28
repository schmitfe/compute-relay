"""Personal project keys remain bound to the same active resource owner."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from compute_relay.control.auth import connection_allowed, token_hash
from compute_relay.control.models import Connection, ProjectCredential, WorkerIdentity
from test_support.unit_test_management import management, human, form_post, key_data, issued


def foreign_key(context):
    return ProjectCredential.objects.create(project=context.project, issued_by=context.outsider, name="foreign agent",
        token_hash=token_hash("synthetic-foreign"), scopes=context.credential.scopes,
        expires_at=timezone.now()+timedelta(days=1))


def test_project_key_cannot_route_through_a_foreign_account_even_if_assigned(management):
    c = management
    key = foreign_key(c)
    worker = WorkerIdentity.objects.create(owner=c.outsider, name="Foreign worker", token_hash=token_hash("synthetic-foreign-worker"))
    connection = Connection.objects.create(owner=c.outsider, worker=worker, name="Foreign HPC login", backend="fake",
        configuration={}, revision_sha256="b"*64)
    c.project.connections.add(connection)
    assert connection_allowed(c.credential, c.connections[0].id)
    assert not connection_allowed(key, connection.id)
    assert not connection_allowed(c.credential, connection.id)
    assert not connection_allowed(key, c.connections[0].id)


def test_mixed_owner_connection_and_worker_are_not_executable(management):
    c = management
    c.project.connections.add(c.foreign)
    assert not connection_allowed(c.credential, c.foreign.id)


def test_foreign_account_cannot_issue_or_rotate_project_execution_keys(management):
    c = management
    user = human(c.outsider)
    assert form_post(user, f"/projects/{c.project.id}/keys/new/", key_data(c)).status_code == 403
    denied = form_post(user, f"/projects/{c.project.id}/keys/{c.credential.id}/rotate/", {"submission_id": str(uuid4())})
    assert denied.status_code == 403
    c.credential.refresh_from_db()
    assert not c.credential.revoked and c.credential.replaced_by_id is None


@pytest.mark.parametrize("change", ["disabled", "foreign_owner"])
def test_existing_key_cannot_outlive_issuer_authority(management, change):
    c = management
    if change == "disabled":
        c.owner.is_active = False
        c.owner.save(update_fields=["is_active"])
    elif change == "foreign_owner":
        c.credential.issued_by = c.outsider
        c.credential.save(update_fields=["issued_by"])
    assert Client().get("/api/v1/connections", HTTP_AUTHORIZATION="Bearer " + c.token).status_code == 403


def test_owner_can_issue_a_read_only_key_without_granting_write_scopes(management):
    c = management
    response = form_post(human(c.owner), f"/projects/{c.project.id}/keys/new/", key_data(c, scopes=["jobs:read"]))
    secret, key = issued(response)
    assert key.issued_by_id == c.owner.id and key.scopes == ["jobs:read"]
    assert Client().post(f"/api/v1/projects/{c.project.id}/objects", {}, content_type="application/json",
        HTTP_AUTHORIZATION="Bearer " + secret).status_code == 403
