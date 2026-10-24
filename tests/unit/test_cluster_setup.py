"""Cluster drafts remain inert until personal setup identifies and confirms them."""

from unittest.mock import patch

import pytest
from django.test import Client

from compute_relay.control.errors import BrokerError
from compute_relay.control.auth import connection_allowed
from compute_relay.control.cluster_services import lease_discovery, report_discovery
from compute_relay.control.forms import ProjectForm
from compute_relay.control.models import ClusterRegistration, Connection, User, WorkerIdentity
from test_management import management, human, form_post, discovery_report, agent_get, queued_discovery


def registration_data(**changes):
    return {"name": "New science cluster", "backend": "slurm", "host": "fixture.invalid", "user": "fixtureuser",
        "port": 22, "remote_root": "/fixture/jobs/new-science", **changes}


def register(context, **changes):
    result = form_post(human(context.owner), "/clusters/new/", registration_data(**changes))
    assert result.status_code == 302
    return ClusterRegistration.objects.get(name=changes.get("name", "New science cluster"))






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
    assert page.status_code == 200 and b"Start your personal connector" in page.content
    assert client.get("/clusters/").status_code == 200


def test_new_directory_is_saved_as_unapproved_intent(management):
    draft = register(management, remote_root="/new/filesystem/science/jobs")
    assert draft.requested_root == "/new/filesystem/science/jobs" and draft.connection_id is None
    assert not Connection.objects.filter(id=draft.id).exists()
    assert str(draft.id).encode() not in agent_get(management.token, "/api/v1/connections").content
    form = ProjectForm(user=management.owner)
    assert not form.fields["connections"].queryset.filter(id=draft.id).exists()


def test_project_form_explains_owned_clusters_waiting_for_confirmation(management):
    pending = Connection.objects.create(owner=management.owner, worker=management.worker,
        name="Pending university cluster", backend="slurm", configuration={}, revision_sha256="a" * 64,
        discovery_state="ready", setup_complete=False, configuration_confirmed=False)
    foreign = Connection.objects.create(owner=management.outsider, worker=management.worker,
        name="Private foreign setup", backend="slurm", configuration={}, revision_sha256="b" * 64,
        discovery_state="ready", setup_complete=False, configuration_confirmed=False)
    client = human(management.owner)
    page = client.get("/projects/new/")
    assert pending.name.encode() in page.content and foreign.name.encode() not in page.content
    assert f'/clusters/{pending.id}/configuration/'.encode() in page.content
    assert not page.context["form"].fields["connections"].queryset.filter(id=pending.id).exists()
    forged = ProjectForm({"connections": [str(pending.id)]}, user=management.owner)
    assert not forged.is_valid() and "connections" in forged.errors




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


def test_foreign_registration_pages_are_not_visible(management):
    draft = register(management)
    stranger = human(management.outsider)
    base = f"/clusters/setup/{draft.id}/"
    for suffix in ["", "edit/"]:
        assert stranger.get(base + suffix).status_code == 404
    assert draft.name.encode() not in stranger.get("/clusters/").content








def test_failed_first_discovery_does_not_unlock_execution(management):
    context = management
    connection = queued_discovery(context)
    connection.setup_complete = False
    connection.save(update_fields=["setup_complete"])
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
    connection = queued_discovery(context)
    connection.setup_complete = False
    connection.save(update_fields=["setup_complete"])
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










def test_stale_edit_cannot_replace_current_personal_setup_target(management):
    context = management
    draft = register(context)
    response = form_post(human(context.owner), f"/clusters/setup/{draft.id}/edit/",
        registration_data(host="updated.invalid", setup_revision=draft.revision))
    assert response.status_code == 302
    replay = form_post(human(context.owner), f"/clusters/setup/{draft.id}/edit/",
        registration_data(setup_revision=draft.revision))
    assert replay.status_code == 409
    draft.refresh_from_db()
    assert draft.host == "updated.invalid" and draft.connection_id is None
