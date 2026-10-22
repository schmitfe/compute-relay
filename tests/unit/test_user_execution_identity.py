from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from compute_broker.control.auth import connection_allowed, token_hash
from compute_broker.control.models import Connection, Membership, ProjectCredential, WorkerIdentity
from test_management import management, human, form_post, key_data, issued


def member_key(context, user, role="submitter"):
    Membership.objects.create(project=context.project, user=user, roles=[role])
    return ProjectCredential.objects.create(project=context.project, issued_by=user, name="member agent",
        token_hash=token_hash("synthetic-member"), scopes=context.credential.scopes,
        expires_at=timezone.now()+timedelta(days=1))


def test_shared_project_routes_each_key_to_its_own_user_account(management):
    c = management
    bob = member_key(c, c.outsider)
    worker = WorkerIdentity.objects.create(owner=c.outsider, name="Bob's worker", token_hash=token_hash("synthetic-bob-worker"))
    connection = Connection.objects.create(owner=c.outsider, worker=worker, name="Bob's HPC login", backend="fake",
        configuration={}, revision_sha256="b"*64)
    c.project.connections.add(connection)
    assert connection_allowed(c.credential, c.connections[0].id)
    assert connection_allowed(bob, connection.id)
    assert not connection_allowed(c.credential, connection.id)
    assert not connection_allowed(bob, c.connections[0].id)


def test_mixed_owner_connection_and_worker_are_not_executable(management):
    c = management
    c.project.connections.add(c.foreign)
    bob = member_key(c, c.outsider)
    assert not connection_allowed(bob, c.foreign.id)


def test_member_can_issue_and_rotate_only_their_own_execution_key(management):
    c = management
    Membership.objects.create(project=c.project, user=c.outsider, roles=["submitter"])
    user = human(c.outsider)
    response = form_post(user, f"/projects/{c.project.id}/keys/new/", key_data(c))
    secret, key = issued(response)
    assert key.issued_by_id == c.outsider.id
    admin = human(c.owner)
    denied = form_post(admin, f"/projects/{c.project.id}/keys/{key.id}/rotate/", {"submission_id": str(uuid4())})
    assert denied.status_code == 403 and secret.encode() not in denied.content
    key.refresh_from_db()
    assert not key.revoked and key.replaced_by_id is None
    rotated = form_post(user, f"/projects/{c.project.id}/keys/{key.id}/rotate/", {"submission_id": str(uuid4())})
    _, replacement = issued(rotated)
    assert replacement.issued_by_id == c.outsider.id


@pytest.mark.parametrize("change", ["disabled", "membership_removed", "owner_unknown"])
def test_existing_key_cannot_outlive_issuer_authority(management, change):
    c = management
    key = member_key(c, c.outsider)
    if change == "disabled":
        c.outsider.is_active = False; c.outsider.save(update_fields=["is_active"])
    elif change == "membership_removed":
        Membership.objects.filter(user=c.outsider, project=c.project).delete()
    else:
        key.issued_by = None; key.save(update_fields=["issued_by"])
    assert Client().get("/api/v1/connections", HTTP_AUTHORIZATION="Bearer synthetic-member").status_code == 403


def test_viewer_cannot_issue_write_scopes_or_broaden_existing_key(management):
    c = management
    member_key(c, c.outsider, "viewer")
    user = human(c.outsider)
    denied = form_post(user, f"/projects/{c.project.id}/keys/new/", key_data(c))
    assert denied.status_code == 403
    read = form_post(user, f"/projects/{c.project.id}/keys/new/", key_data(c, scopes=["jobs:read"]))
    _, key = issued(read)
    assert key.issued_by_id == c.outsider.id and key.scopes == ["jobs:read"]
