"""Cluster details are inert until an owned worker and directory are approved."""

from copy import deepcopy
import json
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import transaction
from django.http import Http404
from django.test import Client

from compute_broker.control import services
from compute_broker.control.errors import BrokerError
from compute_broker.control.auth import connection_allowed, token_hash
from compute_broker.control.cluster_services import lease_discovery, report_discovery
from compute_broker.control.cluster_setup import BindingForm, bind_registration, profile_choice
from compute_broker.control.forms import ProjectForm
from compute_broker.control.models import ClusterRegistration, Connection, User, WorkerIdentity
from test_management import management, human, form_post, discovery_report, agent_get


def registration_data(**changes):
    return {"name": "New science cluster", "backend": "fake", "host": "fixture.invalid", "user": "fixtureuser",
        "port": 22, "remote_root": "/fixture/jobs/new-science", **changes}


def register(context, **changes):
    result = form_post(human(context.owner), "/clusters/new/", registration_data(**changes))
    assert result.status_code == 302
    return ClusterRegistration.objects.get(name=changes.get("name", "New science cluster"))


def binding_data(context, draft, **changes):
    return {"worker": str(context.worker.id), "credential_profile": profile_choice(context.worker.profiles[0]),
        "remote_root": draft.requested_root, "setup_revision": draft.revision, **changes}


def validated(context, draft):
    form = BindingForm(binding_data(context, draft), user=context.owner, registration=draft)
    assert form.is_valid(), form.errors
    return form.cleaned_data


def test_new_cluster_needs_no_worker_or_profile_and_creates_no_connection(management):
    owner = User.objects.create_user(username="new-owner-with-no-worker", password=None)
    client = human(owner)
    page = client.get("/clusters/new/")
    assert page.status_code == 200
    assert "worker" not in page.context["form"].fields and "credential_profile" not in page.context["form"].fields
    before = Connection.objects.count()
    with patch("subprocess.run", side_effect=AssertionError("saving a draft must not run SSH")):
        result = form_post(client, "/clusters/new/", registration_data(user="", remote_root=""))
    assert result.status_code == 302
    draft = ClusterRegistration.objects.get(owner=owner)
    assert draft.connection_id is None and draft.remote_user == draft.requested_root == ""
    assert Connection.objects.count() == before
    page = client.get(result.url)
    assert page.status_code == 200 and b"Worker connection still required" in page.content
    assert client.get("/clusters/").status_code == 200


def test_new_directory_is_saved_as_unapproved_intent(management):
    draft = register(management, remote_root="/new/filesystem/science/jobs")
    assert draft.requested_root == "/new/filesystem/science/jobs" and draft.connection_id is None
    assert not Connection.objects.filter(id=draft.id).exists()
    assert str(draft.id).encode() not in agent_get(management.token, "/api/v1/connections").content
    form = ProjectForm(user=management.owner)
    assert not form.fields["connections"].queryset.filter(id=draft.id).exists()


@pytest.mark.parametrize("changes", [
    {"host": "-oProxyCommand=bad"}, {"host": "host;id"}, {"user": "person;id"},
    {"remote_root": "/"}, {"remote_root": "/tmp/../escape"}, {"remote_root": "/tmp//jobs"},
    {"remote_root": "/tmp/jobs/"}, {"remote_root": "$(id)"}, {"port": 0},
])
def test_draft_rejects_malformed_targets_without_making_executable_state(management, changes):
    response = form_post(human(management.owner), "/clusters/new/", registration_data(**changes))
    assert response.status_code == 200 and response.context["form"].errors
    assert not ClusterRegistration.objects.exists()


def test_setup_mutations_require_a_human_and_csrf(management):
    client = Client(enforce_csrf_checks=True)
    assert client.post("/clusters/new/", registration_data(), HTTP_AUTHORIZATION="Bearer " + management.token).status_code == 403
    owner = human(management.owner)
    assert owner.post("/clusters/new/", registration_data()).status_code == 403
    assert not ClusterRegistration.objects.exists()


def test_foreign_registration_pages_and_templates_are_not_visible(management):
    draft = register(management)
    stranger = human(management.outsider)
    base = f"/clusters/setup/{draft.id}/"
    for suffix in ["", "edit/", "bind/", "worker-config/"]:
        assert stranger.get(base + suffix).status_code == 404
    assert draft.name.encode() not in stranger.get("/clusters/").content
    assert form_post(stranger, base + "bind/", binding_data(management, draft)).status_code == 404


def test_downloaded_template_is_inert_scoped_and_secret_free(management):
    draft = register(management, remote_root="/new/unapproved/jobs")
    with patch("subprocess.run", side_effect=AssertionError("template must not use SSH")):
        response = human(management.owner).get(f"/clusters/setup/{draft.id}/worker-config/")
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    document = json.loads(response.content)
    profile = next(iter(document["credential_profiles"].values()))
    assert profile["remote_root_prefixes"] == [draft.requested_root]
    assert profile["allowed_hosts"] == [draft.host] and profile["allowed_users"] == [draft.remote_user]
    assert profile["site_profile"] == {"automatic_execution_allowed": False}
    assert "token" not in document and document["extensions"] == []
    draft.refresh_from_db()
    assert draft.connection_id is None


def test_template_waits_until_username_and_root_are_set(management):
    draft = register(management, user="", remote_root="")
    assert human(management.owner).get(f"/clusters/setup/{draft.id}/worker-config/").status_code == 409


def test_binding_creates_one_connection_then_discovery_unlocks_project_use(management):
    context = management
    draft = register(context)
    before = Connection.objects.count()
    response = form_post(human(context.owner), f"/clusters/setup/{draft.id}/bind/", binding_data(context, draft))
    assert response.status_code == 302
    draft.refresh_from_db()
    connection = draft.connection
    assert Connection.objects.count() == before + 1
    assert connection.worker_id == context.worker.id and not connection.setup_complete
    assert connection.discovery_state == "queued"
    context.project.connections.add(connection)
    assert not connection_allowed(context.credential, connection.id)
    assert not ProjectForm(user=context.owner).fields["connections"].queryset.filter(id=connection.id).exists()
    assert human(context.owner).get(response.url).status_code == 200
    lease = lease_discovery(context.worker)["lease"]
    assert lease["connection_id"] == str(connection.id)
    report_discovery(context.worker, connection.id, discovery_report(connection, context.worker, lease))
    connection.refresh_from_db()
    assert connection.setup_complete and connection_allowed(context.credential, connection.id)
    assert str(connection.id).encode() in agent_get(context.token, "/api/v1/connections").content
    assert human(context.owner).get(f"/clusters/setup/{draft.id}/").url == f"/clusters/{connection.id}/"


def test_failed_first_discovery_does_not_unlock_execution(management):
    context = management
    draft = register(context)
    connection = bind_registration(context.owner, draft.id, validated(context, draft))
    context.project.connections.add(connection)
    lease = lease_discovery(context.worker)["lease"]
    report_discovery(context.worker, connection.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "outcome": "discovery_failed", "error_code": "AUTHENTICATION_UNAVAILABLE"})
    connection.refresh_from_db()
    assert not connection.setup_complete and not connection_allowed(context.credential, connection.id)
    assert form_post(human(context.owner), f"/clusters/{connection.id}/toggle/", {"enabled": "true"}).status_code == 302
    assert not connection_allowed(context.credential, connection.id)


@pytest.mark.parametrize("change", ["revoked", "withdrawn"])
def test_discovery_rechecks_worker_authority_after_authentication(management, change):
    context = management
    draft = register(context)
    connection = bind_registration(context.owner, draft.id, validated(context, draft))
    lease = lease_discovery(context.worker)["lease"]
    document = discovery_report(connection, context.worker, lease)
    if change == "revoked":
        WorkerIdentity.objects.filter(id=context.worker.id).update(revoked=True)
    else:
        WorkerIdentity.objects.filter(id=context.worker.id).update(profiles=[])
    with pytest.raises(BrokerError):
        report_discovery(context.worker, connection.id, document)
    connection.refresh_from_db()
    assert not connection.setup_complete


@pytest.mark.parametrize("change", ["host", "user", "port", "backend", "root"])
def test_binding_rechecks_target_and_mapping_against_approved_profile(management, change):
    context = management
    changes = {"host": "other.invalid"} if change == "host" else {"user": "otheruser"} if change == "user" else {"port": 2222} if change == "port" else {"backend": "slurm"} if change == "backend" else {"remote_root": "/unapproved/jobs"}
    draft = register(context, **changes)
    response = form_post(human(context.owner), f"/clusters/setup/{draft.id}/bind/", binding_data(context, draft))
    assert response.status_code == 200 and response.context["form"].errors
    draft.refresh_from_db()
    assert draft.connection_id is None


def test_stale_profile_fingerprint_is_rechecked_after_form_validation(management):
    context = management
    draft = register(context)
    data = validated(context, draft)
    changed = deepcopy(context.worker.profiles)
    changed[0]["revision_sha256"] = "f" * 64
    WorkerIdentity.objects.filter(id=context.worker.id).update(profiles=changed)
    with pytest.raises(BrokerError, match="profile changed"):
        bind_registration(context.owner, draft.id, data)
    assert not Connection.objects.filter(registration=draft).exists()


def test_revoked_worker_is_rechecked_after_form_validation(management):
    context = management
    draft = register(context)
    data = validated(context, draft)
    WorkerIdentity.objects.filter(id=context.worker.id).update(revoked=True)
    with pytest.raises(Http404):
        bind_registration(context.owner, draft.id, data)
    draft.refresh_from_db()
    assert draft.connection_id is None


def test_other_owners_worker_cannot_be_bound(management):
    context = management
    draft = register(context)
    worker = WorkerIdentity.objects.create(owner=context.outsider, name="foreign worker", token_hash=token_hash("synthetic-foreign-setup-worker"), profiles=context.worker.profiles)
    response = form_post(human(context.owner), f"/clusters/setup/{draft.id}/bind/", binding_data(context, draft, worker=str(worker.id)))
    assert response.status_code == 404


def test_stale_edit_and_stale_bind_cannot_replace_current_target(management):
    context = management
    draft = register(context)
    old = validated(context, draft)
    response = form_post(human(context.owner), f"/clusters/setup/{draft.id}/edit/",
        registration_data(host="updated.invalid", setup_revision=draft.revision))
    assert response.status_code == 302
    with pytest.raises(BrokerError, match="details changed"):
        bind_registration(context.owner, draft.id, old)
    replay = form_post(human(context.owner), f"/clusters/setup/{draft.id}/edit/",
        registration_data(setup_revision=draft.revision))
    assert replay.status_code == 409
    draft.refresh_from_db()
    assert draft.host == "updated.invalid" and draft.connection_id is None


def test_bound_setup_rejects_duplicate_binding_and_live_target_edit(management):
    context = management
    draft = register(context)
    data = validated(context, draft)
    connection = bind_registration(context.owner, draft.id, data)
    with pytest.raises(BrokerError, match="already connected"):
        bind_registration(context.owner, draft.id, data)
    assert form_post(human(context.owner), f"/clusters/setup/{draft.id}/edit/", registration_data(host="other.invalid")).status_code == 409
    connection.refresh_from_db()
    assert connection.configuration["ssh"]["host"] == "fixture.invalid"


def test_forged_post_target_fields_cannot_override_locked_registration(management):
    context = management
    draft = register(context)
    response = form_post(human(context.owner), f"/clusters/setup/{draft.id}/bind/",
        binding_data(context, draft, host="evil.invalid", user="root", backend="slurm", name="forged"))
    assert response.status_code == 302
    draft.refresh_from_db()
    assert draft.connection.name == draft.name
    assert draft.connection.configuration["ssh"] == {"host": draft.host, "user": draft.remote_user, "port": draft.port}


@pytest.mark.django_db(transaction=True)
def test_competing_bind_requests_create_exactly_one_connection(management):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from django.db import close_old_connections, OperationalError
    context = management
    draft = register(context)
    data = validated(context, draft)
    barrier = threading.Barrier(2)
    before = Connection.objects.count()

    def bind_once():
        close_old_connections()
        barrier.wait(timeout=5)
        try:
            for _ in range(40):
                try:
                    return str(bind_registration(context.owner, draft.id, data).id)
                except BrokerError as exc:
                    return exc.code
                except OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                    # Shared in-memory SQLite can reject BEGIN IMMEDIATE while
                    # the competing writer holds it. Retry the entire operation.
                    time.sleep(.025)
            raise AssertionError("Competing writer did not finish")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: bind_once(), range(2)))
    draft.refresh_from_db()
    assert results.count("SETUP_ALREADY_BOUND") == 1
    assert str(draft.connection_id) in results and Connection.objects.count() == before + 1
