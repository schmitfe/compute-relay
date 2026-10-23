"""Owner-confirmed worker revocation keeps accounting observable and history intact."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection, transaction
from django.test import Client
from django.urls import path
from django.utils import timezone

from compute_broker.control import cluster_services, worker_management
from compute_broker.control.auth import token_hash
from compute_broker.control.errors import BrokerError
from compute_broker.control.identity.models import AuthenticationIdentity
from compute_broker.control.models import (Artifact, Attempt, AuditEvent, ClusterRegistration,
    Connection, ConnectorEnrollment, ConnectorTask, ExecutionRequest, LedgerEntry, Project, User, WorkerIdentity)
from compute_broker.control.urls import urlpatterns as core_urls
from test_management import management, human, form_post, prepared
from test_personal_connector_pairing import setup_cluster, download, enroll


pytestmark = pytest.mark.django_db
urlpatterns = [path("workers/<uuid:worker_id>/remove/", worker_management.worker_remove), *core_urls]


@pytest.fixture(autouse=True)
def removal_urls(settings):
    settings.ROOT_URLCONF = __name__


def removal_path(context):
    return f"/workers/{context.worker.id}/remove/"


def confirmation(context, client=None):
    response = (client or human(context.owner)).get(removal_path(context))
    assert response.status_code == 200
    return {"confirmation": response.context["confirmation"]}


def registration(context, linked=None):
    return ClusterRegistration.objects.create(owner=context.owner, name="Saved cluster setup", host="fixture.invalid",
        remote_user="fixtureuser", requested_root="/fixture/jobs", connection=linked or context.connections[0],
        connector_settings={"identity_file": "/private/worker-key-reference"})


def attempt(context, accounting_state="reserved", *, state="authorized", linked=None):
    job = prepared(context)
    execution = ExecutionRequest.objects.create(job=job, credential=context.credential)
    return Attempt.objects.create(job=job, execution_request=execution, connection=linked or context.connections[0],
        grant=context.grant, state=state, accounting_state=accounting_state,
        reservation_cpu=120, reservation_gpu=0, stdout="retained synthetic stdout")


def test_confirmation_and_detail_are_read_only_owner_scoped_and_do_not_reveal_secrets(management):
    context = management
    current = registration(context)
    before = (WorkerIdentity.objects.count(), Connection.objects.count(), AuditEvent.objects.count())
    client = human(context.owner)
    detail = client.get(f"/workers/{context.worker.id}/")
    assert f"/workers/{context.worker.id}/remove/".encode() in detail.content
    response = client.get(removal_path(context))
    assert response.status_code == 200 and b"Remove worker" in response.content
    assert all(item.name.encode() in response.content for item in context.connections)
    assert context.foreign.name.encode() not in response.content
    assert context.worker.token_hash.encode() not in response.content
    assert b"worker-key-reference" not in response.content
    assert "no-store" in response["Cache-Control"] and "private" in response["Cache-Control"]
    context.worker.refresh_from_db();current.refresh_from_db()
    assert not context.worker.revoked and current.revision == 1
    assert before == (WorkerIdentity.objects.count(), Connection.objects.count(), AuditEvent.objects.count())


def test_removal_revokes_access_disables_links_invalidates_setup_and_preserves_configurations(management):
    context = management
    current = registration(context)
    pending = ConnectorEnrollment.objects.create(registration=current, setup_revision=current.revision,
        token_hash=token_hash("synthetic-pending-pair"), expires_at=timezone.now() + timedelta(minutes=10))
    consumed = ConnectorEnrollment.objects.create(registration=current, worker=context.worker, setup_revision=current.revision,
        token_hash=token_hash("synthetic-consumed-pair"), expires_at=timezone.now(), consumed_at=timezone.now())
    tasks = [ConnectorTask.objects.create(connection=context.connections[0], owner=context.owner, kind="browse", state=state,
        connection_revision=context.connections[0].revision_sha256, lease_id=uuid4(), generation=2,
        expires_at=timezone.now() + timedelta(minutes=10)) for state in ("queued", "running", "succeeded")]
    snapshots = [(item.id, item.configuration, item.revision_sha256) for item in (*context.connections, context.foreign)]
    response = form_post(human(context.owner), removal_path(context), confirmation(context))
    assert response.status_code == 302 and response.url == "/clusters/"
    context.worker.refresh_from_db()
    assert context.worker.revoked
    for identity, configuration, revision in snapshots:
        linked = Connection.objects.get(id=identity)
        assert linked.configuration == configuration and linked.revision_sha256 == revision
        assert not linked.enabled and not linked.setup_complete and not linked.configuration_confirmed
        assert not linked.archived and linked.auth_state == "worker_removed"
        assert linked.discovery_lease_id is None
    assert set(context.project.connections.values_list("id", flat=True)) == {item.id for item in context.connections}
    current.refresh_from_db();pending.refresh_from_db();consumed.refresh_from_db()
    assert current.revision == 2 and not current.archived and current.connection_id == context.connections[0].id
    assert current.connector_settings["identity_file"] == "/private/worker-key-reference"
    assert pending.expires_at <= timezone.now() and pending.consumed_at is None
    assert consumed.consumed_at is not None
    for task in tasks[:2]:
        task.refresh_from_db()
        assert task.state == "failed" and task.error_code == "WORKER_REMOVED" and task.lease_id is None
        assert task.generation == 3
    tasks[-1].refresh_from_db()
    assert tasks[-1].state == "succeeded"
    assert AuditEvent.objects.filter(action="worker.removed", object_id=str(context.worker.id)).count() == 1
    active = human(context.owner).get("/clusters/")
    assert not active.context["workers"].filter(id=context.worker.id).exists()
    assert Client().post("/internal/v1/lease", "{}", content_type="application/json",
        HTTP_AUTHORIZATION="Bearer synthetic-management-worker").status_code == 401


@pytest.mark.parametrize("state,accounting_state", [
    ("authorized", "reserved"), ("submission_uncertain", "submission_uncertain"),
    ("running", "reserved"), ("succeeded", "pending_final"), ("failed", "reserved"),
    ("settlement_pending", "unknown"),
])
def test_any_unsettled_accounting_blocks_removal_even_for_terminal_jobs(management, state, accounting_state):
    context = management
    existing = attempt(context, accounting_state, state=state)
    response = human(context.owner).get(removal_path(context))
    assert response.context["busy"] and b" disabled" in response.content
    denied = form_post(human(context.owner), removal_path(context), {"confirmation": response.context["confirmation"]})
    assert denied.status_code == 409 and denied.context["code"] == "WORKER_BUSY"
    context.worker.refresh_from_db();context.connections[0].refresh_from_db();existing.refresh_from_db()
    assert not context.worker.revoked and context.connections[0].enabled
    assert existing.accounting_state == accounting_state
    assert not AuditEvent.objects.filter(action="worker.removed").exists()


def test_unsettled_job_on_another_owners_connection_still_keeps_worker_available(management):
    attempt(management, linked=management.foreign)
    denied = form_post(human(management.owner), removal_path(management), confirmation(management))
    assert denied.status_code == 409
    assert management.foreign.name.encode() not in denied.content
    management.worker.refresh_from_db()
    assert not management.worker.revoked


@pytest.mark.parametrize("accounting_state", ["settled", "released"])
def test_final_accounting_allows_removal_and_keeps_jobs_artifacts_and_ledger(management, accounting_state):
    context = management
    existing = attempt(context, accounting_state, state="succeeded")
    artifact = Artifact.objects.create(attempt=existing, path="retained.json", blob=existing.job.files.first().blob)
    entry = LedgerEntry.objects.create(grant=context.grant, attempt=existing, action="settle", cpu=8, gpu=0)
    package = existing.job.manifest.copy()
    response = form_post(human(context.owner), removal_path(context), confirmation(context))
    assert response.status_code == 302
    existing.refresh_from_db();artifact.refresh_from_db();entry.refresh_from_db()
    assert existing.accounting_state == accounting_state and existing.stdout == "retained synthetic stdout"
    assert existing.job.manifest == package and artifact.blob_id is not None and entry.cpu == 8


@pytest.mark.parametrize("superuser", [False, True])
def test_other_users_cannot_inspect_or_remove_worker_even_when_admin(management, superuser):
    context = management
    context.outsider.is_superuser = superuser;context.outsider.save()
    client = human(context.outsider)
    assert client.get(removal_path(context)).status_code == 404
    assert form_post(client, removal_path(context), confirmation(context)).status_code == 404
    context.worker.refresh_from_db()
    assert not context.worker.revoked


def test_agent_bearer_anonymous_and_missing_csrf_cannot_remove_worker(management):
    context = management
    payload = confirmation(context)
    agent = Client()
    assert agent.get(removal_path(context), HTTP_AUTHORIZATION="Bearer " + context.token).status_code == 302
    assert agent.post(removal_path(context), payload, HTTP_AUTHORIZATION="Bearer " + context.token).status_code == 403
    csrf = human(context.owner)
    assert csrf.post(removal_path(context), payload).status_code == 403
    context.worker.refresh_from_db()
    assert not context.worker.revoked


def test_expired_or_disabled_directory_identity_cannot_remove_owned_worker(management):
    context = management
    client = human(context.owner)
    payload = confirmation(context)
    AuthenticationIdentity.objects.create(user=context.owner, provider_id="missing-directory-provider", subject=str(uuid4()),
        login_name="synthetic", provider_config_sha256="0" * 64, directory_active=True,
        verified_at=timezone.now() - timedelta(days=1))
    denied_get = client.get(removal_path(context))
    assert denied_get.status_code == 302 and denied_get.url == "/login/"
    assert form_post(client, removal_path(context), payload).status_code == 403
    context.worker.refresh_from_db()
    assert not context.worker.revoked


def test_removal_rechecks_current_identity_inside_transaction(management, monkeypatch):
    context = management
    payload = confirmation(context)
    calls = []
    def current(_user):
        calls.append(transaction.get_connection().in_atomic_block)
        return len(calls) == 1
    monkeypatch.setattr(worker_management, "cached_user_active", current)
    with pytest.raises(BrokerError, match="Current account verification"):
        worker_management.remove_worker(context.owner, context.worker.id, payload["confirmation"])
    assert len(calls) == 2 and calls[-1] is True
    context.worker.refresh_from_db()
    assert not context.worker.revoked


@pytest.mark.parametrize("change", ["missing", "new_connection", "revision", "registration"])
def test_stale_or_missing_confirmation_cannot_remove_new_binding(management, change):
    context = management
    current = registration(context)
    payload = confirmation(context)
    if change == "missing":
        payload = {}
    elif change == "new_connection":
        Connection.objects.create(owner=context.owner, worker=context.worker, name="Added after preview", backend="fake",
            configuration={}, revision_sha256="b" * 64)
    elif change == "revision":
        context.connections[0].revision_sha256 = "f" * 64;context.connections[0].save()
    else:
        current.revision += 1;current.save()
    response = form_post(human(context.owner), removal_path(context), payload)
    assert response.status_code == 409 and response.context["code"] == "WORKER_CHANGED"
    context.worker.refresh_from_db()
    assert not context.worker.revoked


def test_repeated_post_is_idempotent_and_does_not_expire_replacement_pairing(management):
    context = management
    current = registration(context)
    payload = confirmation(context)
    assert form_post(human(context.owner), removal_path(context), payload).status_code == 302
    replacement = ConnectorEnrollment.objects.create(registration=current, setup_revision=2,
        token_hash=token_hash("synthetic-new-pair"), expires_at=timezone.now() + timedelta(minutes=10))
    expiry = replacement.expires_at
    assert form_post(human(context.owner), removal_path(context), payload).status_code == 302
    replacement.refresh_from_db();current.refresh_from_db()
    assert replacement.expires_at == expiry and current.revision == 2
    assert AuditEvent.objects.filter(action="worker.removed", object_id=str(context.worker.id)).count() == 1
    detail = human(context.owner).get(f"/workers/{context.worker.id}/")
    assert b"Removed" in detail.content and b">Remove worker</a>" not in detail.content


def test_repair_after_removal_reuses_configuration_and_requires_discovery_and_confirmation(setup_cluster):
    owner, current, client = setup_cluster
    first = enroll(setup_cluster, download(setup_cluster)).json()
    linked = Connection.objects.get(id=first["connection_id"])
    expired_setup = download(setup_cluster)
    remove_url = f"/workers/{first['worker_id']}/remove/"
    preview = client.get(remove_url)
    assert preview.status_code == 200
    assert client.post(remove_url, {"confirmation": preview.context["confirmation"]}).status_code == 302
    assert enroll(setup_cluster, expired_setup).status_code == 409
    fresh_setup = download(setup_cluster)
    replaced = enroll(setup_cluster, fresh_setup)
    assert replaced.status_code == 201
    linked.refresh_from_db()
    assert linked.id == Connection.objects.get().id
    assert str(linked.worker_id) == replaced.json()["worker_id"] != first["worker_id"]
    assert linked.enabled and not linked.setup_complete and not linked.configuration_confirmed
    assert linked.discovery_state == "queued" and linked.auth_state == "discovery_required"
    assert linked.configuration["site_profile"]["automatic_execution_allowed"] is False
    assert WorkerIdentity.objects.get(id=first["worker_id"]).revoked
    assert not linked.archived and not ClusterRegistration.objects.get(id=current.id).archived


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_other_http_methods_cannot_remove_worker(management, method):
    client = Client();client.force_login(management.owner)
    assert getattr(client, method)(removal_path(management)).status_code == 405
    management.worker.refresh_from_db()
    assert not management.worker.revoked


@pytest.mark.django_db(transaction=True)
def test_removal_waits_for_project_admission_and_rechecks_new_unsettled_attempt(management):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    context = management
    job = prepared(context)
    execution = ExecutionRequest.objects.create(job=job, credential=context.credential)
    payload = confirmation(context)
    project_locked, removal_waiting = Event(), Event()

    def admit():
        close_old_connections()
        try:
            with transaction.atomic():
                Project.objects.select_for_update().get(id=context.project.id)
                project_locked.set()
                assert removal_waiting.wait(5)
                Attempt.objects.create(job=job, execution_request=execution, connection=context.connections[0],
                    grant=context.grant, reservation_cpu=120, reservation_gpu=0)
        finally:
            close_old_connections()

    def remove():
        close_old_connections()
        try:
            def synchronize(execute, sql, params, many, wrapper_context):
                if "BROKER_PROJECT" in sql.upper() and "FOR UPDATE" in sql.upper():
                    removal_waiting.set()
                return execute(sql, params, many, wrapper_context)
            with transaction.atomic(), connection.execute_wrapper(synchronize):
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '8s'")
                    cursor.execute("SET LOCAL statement_timeout = '12s'")
                try:
                    worker_management.remove_worker(context.owner, context.worker.id, payload["confirmation"])
                except BrokerError as error:
                    return error.code
                return "removed"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        admitted = executor.submit(admit)
        assert project_locked.wait(5)
        removal = executor.submit(remove)
        admitted.result(timeout=20)
        assert removal.result(timeout=20) == "WORKER_BUSY"
    context.worker.refresh_from_db()
    assert not context.worker.revoked
    assert not AuditEvent.objects.filter(action="worker.removed").exists()


@pytest.mark.django_db(transaction=True)
def test_removal_waits_for_worker_before_locking_discovery_connections(management):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    context = management
    linked = context.connections[0]
    linked.discovery_state = "queued";linked.save()
    lease = cluster_services.lease_discovery(context.worker)["lease"]
    report = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "discovery_failed",
        "error_code": "SYNTHETIC_UNAVAILABLE"}
    payload = confirmation(context)
    worker_locked, removal_waiting = Event(), Event()

    def discover():
        close_old_connections()
        try:
            with transaction.atomic():
                worker = WorkerIdentity.objects.select_for_update().get(id=context.worker.id)
                worker_locked.set()
                assert removal_waiting.wait(5)
                return cluster_services.report_discovery(worker, lease["connection_id"], report)
        finally:
            close_old_connections()

    def remove():
        close_old_connections()
        try:
            def synchronize(execute, sql, params, many, wrapper_context):
                if "BROKER_WORKERIDENTITY" in sql.upper() and "FOR UPDATE" in sql.upper():
                    removal_waiting.set()
                return execute(sql, params, many, wrapper_context)
            with transaction.atomic(), connection.execute_wrapper(synchronize):
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '8s'")
                    cursor.execute("SET LOCAL statement_timeout = '12s'")
                return worker_management.remove_worker(context.owner, context.worker.id, payload["confirmation"])
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        discovery = executor.submit(discover)
        assert worker_locked.wait(5)
        removal = executor.submit(remove)
        assert discovery.result(timeout=20) == {"accepted": True, "state": "failed"}
        assert removal.result(timeout=20) is True
    context.worker.refresh_from_db()
    assert context.worker.revoked
