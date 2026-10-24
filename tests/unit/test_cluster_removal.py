"""Human removal archives configuration without touching jobs or remote files."""

from datetime import timedelta
import json
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import include, path
from django.utils import timezone

from compute_relay.control import cluster_removal
from compute_relay.control.auth import token_hash
from compute_relay.control.models import (Artifact, Attempt, AuditEvent, ClusterRegistration,
    Connection, ConnectorEnrollment, ExecutionRequest, Job, User, WorkerIdentity)
from test_management import management, prepared


urlpatterns = [
    path("clusters/setup/<uuid:registration_id>/remove/", cluster_removal.registration),
    path("clusters/<uuid:connection_id>/remove/", cluster_removal.connection),
    path("", include("compute_relay.control.urls")),
]


@pytest.fixture(autouse=True)
def removal_routes(settings):
    # The feature owns handlers only; the application's URL wiring is integrated
    # separately. Keep these tests valid before and after that wiring lands.
    settings.ROOT_URLCONF = __name__


def human(user):
    client = Client();client.force_login(user)
    return client


def registration(context, connection=None):
    return ClusterRegistration.objects.create(owner=context.owner, name="Synthetic cluster", host="cluster.example.invalid",
        remote_user="synthetic", requested_root="/srv/jobs", connection=connection,
        connector_settings={"identity_file": "/private/opaque-key-path", "session_seconds": 60})


def fences(registration=None, connection=None):
    return {**({"setup_revision": str(registration.revision)} if registration else {}),
        **({"connection_revision_sha256": connection.revision_sha256} if connection else {})}


def pairing(registration, *, worker=None, consumed=False):
    return ConnectorEnrollment.objects.create(registration=registration, token_hash=token_hash(str(uuid4())),
        setup_revision=registration.revision, expires_at=timezone.now() + timedelta(minutes=10),
        worker=worker, consumed_at=timezone.now() if consumed else None)


def attempt(context, accounting_state):
    job = prepared(context)
    execution = ExecutionRequest.objects.create(job=job, credential=context.credential)
    current = Attempt.objects.create(job=job, execution_request=execution, connection=context.connections[0], grant=context.grant,
        reservation_cpu=0, reservation_gpu=0, state="completed", accounting_state=accounting_state,
        stdout="retained synthetic stdout")
    return current


def test_get_confirmation_has_no_side_effects_or_secret_hints(management):
    context = management;current = registration(context)
    pending = pairing(current)
    before = AuditEvent.objects.count()
    response = human(context.owner).get(f"/clusters/setup/{current.id}/remove/")
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    assert response.context["setup_revision"] == str(current.revision)
    assert b"No files" in response.content and b"accounting history" in response.content
    assert b"opaque-key-path" not in response.content
    current.refresh_from_db();pending.refresh_from_db()
    assert not current.archived and current.revision == 1 and current.connector_settings
    assert pending.expires_at > timezone.now() and AuditEvent.objects.count() == before


def test_unbound_registration_removal_expires_pairings_and_clears_hints(management):
    context = management;current = registration(context)
    pending = pairing(current)
    response = human(context.owner).post(f"/clusters/setup/{current.id}/remove/", fences(current))
    assert response.status_code == 302 and response["Location"] == "/clusters/"
    current.refresh_from_db();pending.refresh_from_db()
    assert current.archived and current.connector_settings == {} and current.revision == 2
    assert pending.expires_at <= timezone.now()
    assert Connection.objects.count() == 3 and not context.worker.revoked
    event = AuditEvent.objects.get(action="cluster.removed")
    assert event.object_id == str(current.id)
    assert "opaque-key-path" not in json.dumps(list(AuditEvent.objects.values()), default=str)


@pytest.mark.parametrize("route", ["registration", "connection"])
def test_bound_removal_archives_both_and_revokes_exclusive_personal_worker(management, route):
    context = management;linked = context.connections[0]
    personal = WorkerIdentity.objects.create(owner=context.owner, name="Exclusive personal", token_hash=token_hash(str(uuid4())))
    linked.worker = personal
    linked.discovery_state, linked.discovery_error, linked.auth_state = "discovering", "OLD_ERROR", "ready"
    linked.discovery_lease_id, linked.discovery_lease_expires_at = uuid4(), timezone.now() + timedelta(minutes=1)
    linked.save()
    current = registration(context, linked)
    consumed = pairing(current, worker=personal, consumed=True);pending = pairing(current)
    job = prepared(context);manifest = job.manifest.copy();configuration = linked.configuration.copy()
    url = f"/clusters/setup/{current.id}/remove/" if route == "registration" else f"/clusters/{linked.id}/remove/"
    response = human(context.owner).post(url, fences(current, linked))
    assert response.status_code == 302
    linked.refresh_from_db();current.refresh_from_db();personal.refresh_from_db();pending.refresh_from_db();consumed.refresh_from_db()
    assert linked.archived and not linked.enabled and not linked.setup_complete and linked.auth_state == "removed"
    assert linked.discovery_state == "not_started" and linked.discovery_error == ""
    assert linked.discovery_lease_id is None and linked.discovery_lease_expires_at is None
    assert current.archived and current.connector_settings == {} and current.revision == 2
    assert personal.revoked and pending.expires_at <= timezone.now() and consumed.consumed_at is not None
    assert linked.configuration == configuration and context.project.connections.filter(pk=linked.id).exists()
    assert Job.objects.get(pk=job.id).manifest == manifest
    assert AuditEvent.objects.filter(action="cluster.removed").count() == 1


def test_shared_fixture_worker_is_never_revoked_by_one_configuration_removal(management):
    context = management;linked = context.connections[0]
    context.connections[1].archived = True;context.connections[1].save()
    context.foreign.archived = True;context.foreign.save()
    response = human(context.owner).post(f"/clusters/{linked.id}/remove/", fences(connection=linked))
    assert response.status_code == 302
    context.worker.refresh_from_db()
    assert not context.worker.revoked


def test_personal_worker_with_another_active_connection_stays_available(management):
    context = management;linked = context.connections[0]
    current = registration(context, linked);pairing(current, worker=context.worker, consumed=True)
    response = human(context.owner).post(f"/clusters/{linked.id}/remove/", fences(current, linked))
    assert response.status_code == 302
    context.worker.refresh_from_db();context.connections[1].refresh_from_db()
    assert not context.worker.revoked and not context.connections[1].archived


@pytest.mark.parametrize("accounting_state", ["reserved", "pending_final", "submission_uncertain"])
def test_even_terminal_jobs_block_removal_until_accounting_is_final(management, accounting_state):
    context = management;linked = context.connections[0];current = registration(context, linked)
    existing = attempt(context, accounting_state)
    response = human(context.owner).get(f"/clusters/{linked.id}/remove/")
    assert response.context["busy"] and b" disabled" in response.content
    response = human(context.owner).post(f"/clusters/{linked.id}/remove/", fences(current, linked))
    assert response.status_code == 409 and response.context["code"] == "CONNECTION_BUSY"
    linked.refresh_from_db();current.refresh_from_db();existing.refresh_from_db()
    assert not linked.archived and not current.archived and existing.accounting_state == accounting_state
    assert not AuditEvent.objects.filter(action="cluster.removed").exists()


@pytest.mark.parametrize("accounting_state", ["settled", "released"])
def test_final_accounting_allows_removal_and_keeps_artifact_and_job_history(management, accounting_state):
    context = management;linked = context.connections[0];existing = attempt(context, accounting_state)
    artifact = Artifact.objects.create(attempt=existing, path="retained.txt", blob=existing.job.files.first().blob)
    manifest = existing.job.manifest.copy()
    response = human(context.owner).post(f"/clusters/{linked.id}/remove/", fences(connection=linked))
    assert response.status_code == 302
    existing.refresh_from_db();artifact.refresh_from_db()
    assert existing.accounting_state == accounting_state and existing.stdout == "retained synthetic stdout"
    assert existing.job.manifest == manifest and artifact.blob_id is not None


@pytest.mark.parametrize("change", ["setup", "connection", "missing_setup", "missing_connection"])
def test_stale_or_incomplete_bound_confirmation_is_rejected(management, change):
    context = management;linked = context.connections[0];current = registration(context, linked)
    posted = fences(current, linked)
    if change == "setup": current.revision += 1;current.save()
    elif change == "connection": linked.revision_sha256 = "b" * 64;linked.save()
    elif change == "missing_setup": posted.pop("setup_revision")
    else: posted.pop("connection_revision_sha256")
    response = human(context.owner).post(f"/clusters/{linked.id}/remove/", posted)
    assert response.status_code == 409 and response.context["code"] == "SETUP_CHANGED"
    linked.refresh_from_db();current.refresh_from_db()
    assert not linked.archived and not current.archived


def test_new_registration_after_connection_confirmation_requires_new_confirmation(management):
    context = management;linked = context.connections[0]
    posted = fences(connection=linked)
    current = registration(context, linked)
    response = human(context.owner).post(f"/clusters/{linked.id}/remove/", posted)
    assert response.status_code == 409
    current.refresh_from_db();assert not current.archived


@pytest.mark.parametrize("route", ["registration", "connection"])
def test_replayed_removal_does_not_mutate_history_or_audit_twice(management, route):
    context = management;linked = context.connections[0];current = registration(context, linked)
    url = f"/clusters/setup/{current.id}/remove/" if route == "registration" else f"/clusters/{linked.id}/remove/"
    posted = fences(current, linked);client = human(context.owner)
    assert client.post(url, posted).status_code == 302
    response = client.post(url, posted)
    assert response.status_code == 409 and response.context["code"] == "CLUSTER_REMOVED"
    assert AuditEvent.objects.filter(action="cluster.removed").count() == 1


def test_only_owner_human_can_confirm_with_csrf(management):
    context = management;linked = context.connections[0];current = registration(context, linked)
    admin = User.objects.create_superuser(username="removal-other-admin", password="synthetic")
    for url in (f"/clusters/setup/{current.id}/remove/", f"/clusters/{linked.id}/remove/"):
        assert human(context.outsider).get(url).status_code == 404
        assert human(admin).post(url, fences(current, linked)).status_code == 404
        assert Client().post(url, fences(current, linked), HTTP_AUTHORIZATION="Bearer " + context.token).status_code == 403
        csrf = Client(enforce_csrf_checks=True);csrf.force_login(context.owner)
        assert csrf.post(url, fences(current, linked)).status_code == 403
        assert human(context.owner).delete(url).status_code == 405
    linked.refresh_from_db();assert not linked.archived


def test_get_and_post_require_current_owner_authority(management, monkeypatch):
    context = management;linked = context.connections[0];client = human(context.owner)
    monkeypatch.setattr(cluster_removal, "current_user_active", lambda user: False)
    assert client.get(f"/clusters/{linked.id}/remove/").status_code == 403
    assert client.post(f"/clusters/{linked.id}/remove/", fences(connection=linked)).status_code == 403
    linked.refresh_from_db();assert not linked.archived


def test_owner_authority_is_rechecked_inside_removal_transaction(management, monkeypatch):
    context = management;linked = context.connections[0];checks = iter([True, False])
    monkeypatch.setattr(cluster_removal, "current_user_active", lambda user: next(checks))
    response = human(context.owner).post(f"/clusters/{linked.id}/remove/", fences(connection=linked))
    assert response.status_code == 403
    linked.refresh_from_db();assert not linked.archived
