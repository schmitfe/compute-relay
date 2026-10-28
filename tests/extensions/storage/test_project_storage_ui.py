"""Project navigation and human filesystem receipts never cross project boundaries."""

from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client

from compute_relay.control.models import ConnectorTask, Project
from compute_relay.extensions.storage import services
from compute_relay.extensions.storage.models import StorageTarget, StorageTargetRegistration
from test_support.extensions_storage_test_personal_storage_targets import complete, document, personal, queue, target_path
from test_support.extensions_storage_test_storage_control import enroll


def human(user, *, csrf=False):
    client = Client(enforce_csrf_checks=csrf)
    client.force_login(user)
    return client


def other_project(resources, *, owner=None, name="Second storage project"):
    project = Project.objects.create(owner=owner or resources.owner, name=name)
    project.connections.add(resources.connection)
    return project


def test_global_overview_is_a_project_chooser_without_remote_paths(resources):
    enroll(resources)
    foreign = other_project(resources, owner=resources.outsider, name="Hidden foreign project")
    response = human(resources.owner).get("/storage/targets/")
    assert response.status_code == 200
    assert resources.project.name.encode() in response.content
    assert f'/projects/{resources.project.id}/storage/'.encode() in response.content
    assert foreign.name.encode() not in response.content
    assert b"/approved/project-space" not in response.content
    assert b"/storage/targets/new/" not in response.content
    assert b"1 location" in response.content


def test_creation_and_directory_operations_require_project_routes(resources):
    client = human(resources.owner)
    assert client.get("/storage/targets/new/").status_code == 404
    assert client.get(f"/storage/targets/new/?project={resources.project.id}&connection={resources.connection.id}").status_code == 404
    assert client.get(f"/storage/targets/directories/{resources.connection.id}/").status_code == 404
    response = client.post("/storage/targets/new/", {"name": "Unscoped", "connection": resources.connection.id,
        "root_alias": "staging-root-0"})
    assert response.status_code == 404
    assert not ConnectorTask.objects.exists() and not StorageTarget.objects.exists()


def test_project_location_table_and_default_choices_exclude_other_projects(resources):
    first = enroll(resources)
    second_project = other_project(resources)
    foreign = services.register_target(resources.owner, resources.connection, "staging-root-0", "Other project secret location",
        project=second_project)
    page = human(resources.owner).get(f"/projects/{resources.project.id}/storage/")
    assert page.status_code == 200
    assert first.name.encode() in page.content and foreign.name.encode() not in page.content
    assert str(first.id).encode() in page.content and str(foreign.id).encode() not in page.content
    assert b"Input data location" in page.content
    assert f'/projects/{resources.project.id}/storage/targets/new/'.encode() in page.content


@pytest.mark.parametrize("explicit_fields", [("project_id",), ("project",), ("project", "project_id")])
def test_forged_form_project_context_is_rejected_before_queueing(personal, explicit_fields):
    other = other_project(personal)
    payload = document(personal)
    payload.update({field: str(other.id) for field in explicit_fields})
    if len(explicit_fields) == 2:
        payload["project"] = str(personal.project.id)
    response = personal.human.post(target_path(personal, "new/"), payload)
    assert response.status_code == 403
    assert not ConnectorTask.objects.exists() and not StorageTargetRegistration.objects.exists()


def test_unassigned_cluster_is_hidden_and_cannot_queue_browser_work(personal):
    personal.project.connections.clear()
    page = personal.human.get(target_path(personal, f"new/?connection={personal.connection.id}"))
    assert page.status_code == 200 and personal.connection.name.encode() not in page.content
    response = personal.human.post(target_path(personal, "new/"), document(personal))
    assert response.status_code == 200
    assert personal.human.post(target_path(personal, f"directories/{personal.connection.id}/"),
        {"path": "/scratch", "connection_revision": personal.connection.revision_sha256}).status_code == 404
    assert not ConnectorTask.objects.exists()


def test_foreign_account_cannot_use_another_owners_ssh_identity(personal):
    client = human(personal.outsider)
    page = client.get(target_path(personal, f"new/?connection={personal.connection.id}"))
    assert page.status_code == 403 and personal.connection.name.encode() not in page.content
    assert client.post(target_path(personal, f"directories/{personal.connection.id}/"),
        {"path": "/scratch", "connection_revision": personal.connection.revision_sha256}).status_code == 403
    assert not ConnectorTask.objects.exists()


def test_browser_receipt_is_bound_to_project_and_retains_the_enrollment_nonce(personal):
    payload = document(personal, action="browse_directory", roles=["data", "results"])
    response = personal.human.post(target_path(personal, "new/"), payload)
    retained = parse_qs(urlsplit(response.url).query)
    assert retained["submission_id"] == [payload["submission_id"]]
    assert retained["roles"] == ["data", "results"]
    response = personal.human.post(target_path(personal, f"directories/{personal.connection.id}/"),
        {**payload, "path": "/scratch/researcher"})
    task = ConnectorTask.objects.get()
    assert task.request == {"path": "/scratch/researcher", "storage_project_id": str(personal.project.id)}
    task.state = "succeeded"
    task.result = {"ok": True, "path": "/scratch/researcher", "entries": [], "writable": True}
    task.save(update_fields=["state", "result"])
    page = personal.human.get(response.url)
    assert payload["submission_id"].encode() in page.content
    assert f'name="project_id" value="{personal.project.id}"'.encode() in page.content
    second = other_project(personal)
    cross_project = f"/projects/{second.id}/storage/targets/directories/{personal.connection.id}/?task={task.id}"
    assert personal.human.get(cross_project).status_code == 404
    assert ConnectorTask.objects.count() == 1 and not StorageTarget.objects.exists()


def test_registration_receipt_cannot_be_confirmed_in_another_project(personal):
    registration = queue(personal)
    assert complete(personal, registration).status_code == 200
    second = other_project(personal)
    forged = f"/projects/{second.id}/storage/targets/registrations/{registration.id}/"
    assert personal.human.get(forged).status_code == 404
    assert personal.human.post(forged, {"confirmed_root": registration.root}).status_code == 404
    registration.refresh_from_db()
    assert registration.project_id == personal.project.id and not registration.target_id
    assert not StorageTarget.objects.exists()
    assert personal.human.get(f"/storage/targets/registrations/{registration.id}/").status_code == 404
