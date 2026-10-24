"""Personal roots require a human selection and the exact fenced worker result."""

from datetime import timedelta
import json
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from compute_relay.control.models import ClusterRegistration, ConnectorEnrollment, ConnectorTask, WorkerIdentity
from compute_relay.control.auth import token_hash
from compute_relay.extensions.storage import services
from compute_relay.extensions.storage.models import StorageTarget, StorageTargetRegistration


@pytest.fixture
def personal(resources):
    resources.profile["storage_roots"] = []
    resources.worker.profiles = [resources.profile]
    resources.worker.connector_features = ["personal_setup_v1", "personal_folders_v1"]
    resources.worker.save(update_fields=["profiles", "connector_features"])
    registration = ClusterRegistration.objects.create(owner=resources.owner, name="Personal storage cluster",
        host="storage.invalid", remote_user="researcher", connection=resources.connection)
    ConnectorEnrollment.objects.create(registration=registration, worker=resources.worker, setup_revision=1,
        token_hash=token_hash("pairing-storage"), consumed_at=timezone.now(),
        expires_at=timezone.now() + timedelta(minutes=5))
    resources.human = Client()
    resources.human.force_login(resources.owner)
    return resources


def document(personal, **changes):
    return {"connection": str(personal.connection.id), "connection_revision": personal.connection.revision_sha256,
        "name": "Project results", "root_path": "/scratch/researcher/results", "roles": ["work", "results"],
        "submission_id": str(uuid4()), **changes}


def target_path(personal, suffix=""):
    return f"/projects/{personal.project.id}/storage/targets/" + suffix


def queue(personal, **changes):
    response = personal.human.post(target_path(personal, "new/"), document(personal, **changes))
    assert response.status_code == 302, response.content
    return StorageTargetRegistration.objects.latest("created_at")


def worker_post(personal, path, payload):
    return Client().post(path, json.dumps(payload), content_type="application/json",
        HTTP_AUTHORIZATION="Bearer storage-worker")


def complete(personal, registration, **changes):
    response = worker_post(personal, "/internal/v1/connectors/tasks", {"connection_id": str(personal.connection.id)})
    assert response.status_code == 200, response.content
    task = response.json()["task"]
    assert task["id"] == str(registration.task_id)
    result = {"ok": True, "path": registration.root, "parent": "/scratch/researcher", "home": "/home/researcher",
        "entries": [], "writable": True, "truncated": False, "scanned_entries": 0, "created": True}
    return worker_post(personal, f"/internal/v1/connectors/tasks/{task['id']}/report", {
        "connection_id": str(personal.connection.id), "lease_id": task["lease_id"], "generation": task["generation"],
        "outcome": "succeeded", "result": result, **changes})


def save(personal, registration, *, confirmed_root=None):
    registration.task.refresh_from_db()
    root = registration.task.result.get("path") if confirmed_root is None else confirmed_root
    return personal.human.post(target_path(personal, f"registrations/{registration.id}/"), {"confirmed_root": root or ""})


def test_personal_form_does_not_require_operator_roots_and_gets_do_not_queue_work(personal):
    response = personal.human.get(target_path(personal, f"new/?connection={personal.connection.id}"))
    assert response.status_code == 200
    assert b"Check/create directory" in response.content and b"Browse directories" in response.content
    assert b"native quota remain unknown" in response.content
    assert not ConnectorTask.objects.exists() and not StorageTargetRegistration.objects.exists()
    response = personal.human.get(target_path(personal, f"directories/{personal.connection.id}/"))
    assert response.status_code == 200 and not ConnectorTask.objects.exists()


@pytest.mark.parametrize("created", [False, True])
def test_exact_personal_directory_can_be_saved_and_used_for_advisory_project_inventory(personal, created):
    registration = queue(personal)
    assert registration.task.kind == "mkdir"
    assert registration.task.request == {"path": "/scratch/researcher", "name": "results"}
    assert not StorageTarget.objects.exists()
    result = {"ok": True, "path": registration.root, "parent": "/scratch/researcher", "entries": [],
        "writable": True, "created": created}
    assert complete(personal, registration, result=result).status_code == 200
    page = personal.human.get(target_path(personal, f"registrations/{registration.id}/"))
    assert b"Save storage location" in page.content and not StorageTarget.objects.exists()
    assert save(personal, registration).status_code == 302
    assert save(personal, registration).status_code == 302
    target = StorageTarget.objects.get()
    assert target.root == registration.root and target.roles == ["work", "results"]
    assert services.target_current(target)
    assert target.pool.probe_enabled is False and target.pool.expected_filesystem_id is None
    assert target.capabilities["quota_reporting"] == "unknown"
    services.save_project_storage(personal.owner, personal.project, personal.connection,
        {"results": target, "retained_bytes_estimate": 0, "retained_inodes_estimate": 0})
    inventory = services.inventory(personal.project, credential=personal.credential)
    assert inventory["bindings"][0]["targets"]["results"] == str(target.id)
    assert inventory["pools"][0]["probe_supported"] is False
    assert inventory["pools"][0]["measurement_status"] == "not_observed"
    assert "root" not in inventory["targets"][0]
    assert personal.human.post(f"/projects/{personal.project.id}/storage/pools/{target.pool_id}/probes/", {"enabled": "true"}).status_code == 409
    assert b"Owner-selected directory" in personal.human.get(f"/projects/{personal.project.id}/storage/").content


def test_repeated_check_form_is_idempotent_and_different_payload_conflicts(personal):
    payload = document(personal)
    first = personal.human.post(target_path(personal, "new/"), payload)
    second = personal.human.post(target_path(personal, "new/"), payload)
    assert first.status_code == second.status_code == 302 and first.url == second.url
    assert ConnectorTask.objects.count() == StorageTargetRegistration.objects.count() == 1
    assert personal.human.post(target_path(personal, "new/"), {**payload, "root_path": "/scratch/researcher/different"}).status_code == 409


def test_canonical_parent_alias_resolution_is_displayed_and_explicitly_confirmed(personal):
    registration = queue(personal, root_path="/home-alias/researcher/results")
    resolved = "/network/home/researcher/results"
    receipt = {"ok": True, "path": resolved, "parent": "/network/home/researcher", "entries": [],
        "writable": True, "created": False}
    assert complete(personal, registration, result=receipt).status_code == 200
    page = personal.human.get(target_path(personal, f"registrations/{registration.id}/"))
    assert b"/home-alias/researcher/results" in page.content and resolved.encode() in page.content
    assert b"cluster resolved the parent directory" in page.content
    assert f'name="confirmed_root" value="{resolved}"'.encode() in page.content
    assert save(personal, registration, confirmed_root=registration.root).status_code == 409
    assert not StorageTarget.objects.exists()
    assert save(personal, registration, confirmed_root=resolved).status_code == 302
    target = StorageTarget.objects.get()
    assert target.root == target.pool.probe_root == resolved
    registration.refresh_from_db()
    assert registration.root == "/home-alias/researcher/results"
    assert services.target_current(target)


@pytest.mark.parametrize("confirmation", [None, "/scratch/researcher/other", "/scratch/../researcher/results"])
def test_final_save_requires_the_exact_displayed_canonical_confirmation(personal, confirmation):
    registration = queue(personal)
    assert complete(personal, registration).status_code == 200
    payload = {} if confirmation is None else {"confirmed_root": confirmation}
    assert personal.human.post(target_path(personal, f"registrations/{registration.id}/"), payload).status_code == 409
    assert not StorageTarget.objects.exists()


@pytest.mark.parametrize("change", [
    {"path": "/elsewhere/results"}, {"parent": "/elsewhere"}, {"writable": False},
    {"ok": False}, {"created": "true"},
])
def test_unbound_or_malformed_worker_receipt_cannot_register_another_root(personal, change):
    registration = queue(personal)
    result = {"ok": True, "path": registration.root, "parent": "/scratch/researcher", "entries": [],
        "writable": True, "created": True, **change}
    response = complete(personal, registration, result=result)
    assert response.status_code in {200, 400}
    assert save(personal, registration).status_code == 409 and not StorageTarget.objects.exists()


def test_failure_missing_parent_and_expiry_do_not_save_or_repeat_remote_work(personal):
    registration = queue(personal)
    assert complete(personal, registration, outcome="failed", error_code="PATH_UNAVAILABLE", result={}).status_code == 200
    assert save(personal, registration).status_code == 409
    assert b"PATH_UNAVAILABLE" in personal.human.get(target_path(personal, f"registrations/{registration.id}/")).content
    registration.task.expires_at = timezone.now() - timedelta(seconds=1)
    registration.task.save(update_fields=["expires_at"])
    assert b"expired" in personal.human.get(target_path(personal, f"registrations/{registration.id}/")).content
    assert ConnectorTask.objects.count() == 1 and not StorageTarget.objects.exists()


def test_stale_profile_connection_or_foreign_task_is_rejected(personal):
    registration = queue(personal)
    assert complete(personal, registration).status_code == 200
    personal.connection.revision_sha256 = "b" * 64
    personal.connection.save(update_fields=["revision_sha256"])
    assert save(personal, registration).status_code == 409 and not StorageTarget.objects.exists()
    personal.connection.revision_sha256 = registration.connection_revision_sha256
    personal.connection.save(update_fields=["revision_sha256"])
    registration.task.owner = personal.outsider
    registration.task.save(update_fields=["owner"])
    assert save(personal, registration).status_code == 409 and not StorageTarget.objects.exists()


def test_saved_target_becomes_stale_after_worker_repair_or_revocation(personal):
    registration = queue(personal)
    assert complete(personal, registration).status_code == 200
    assert save(personal, registration).status_code == 302
    target = StorageTarget.objects.get()
    personal.worker.revoked = True
    personal.worker.save(update_fields=["revoked"])
    target.connection.worker.refresh_from_db()
    assert not services.target_current(target)
    replacement = WorkerIdentity.objects.create(owner=personal.owner, name="replacement", token_hash=token_hash("new-storage-worker"),
        profiles=[personal.profile])
    personal.connection.worker = replacement
    personal.connection.save(update_fields=["worker"])
    target.refresh_from_db()
    assert not services.target_current(target)


def test_personal_browser_preserves_name_roles_and_selection_without_get_mutations(personal):
    payload = document(personal, action="browse_directory")
    response = personal.human.post(target_path(personal, "new/"), payload)
    assert response.status_code == 302 and "name=Project+results" in response.url
    assert "roles=work" in response.url and "roles=results" in response.url
    page = personal.human.get(response.url)
    assert b'value="Project results"' in page.content and not ConnectorTask.objects.exists()
    response = personal.human.post(target_path(personal, f"directories/{personal.connection.id}/"), {
        **payload, "path": "/scratch/researcher"})
    assert response.status_code == 302 and "roles=results" in response.url
    task = ConnectorTask.objects.get()
    task.state = "succeeded"
    task.result = {"ok": True, "path": "/scratch/researcher", "parent": "/scratch", "entries": [], "writable": True}
    task.save(update_fields=["state", "result"])
    page = personal.human.get(response.url)
    assert b"Use this storage directory" in page.content and b"root_path=%2Fscratch%2Fresearcher" in page.content
    assert ConnectorTask.objects.count() == 1 and not StorageTarget.objects.exists()


def test_new_human_operations_require_csrf_and_never_accept_project_keys(personal):
    client = Client(enforce_csrf_checks=True)
    client.force_login(personal.owner)
    assert client.post(target_path(personal, "new/"), document(personal)).status_code == 403
    assert client.post(target_path(personal, f"directories/{personal.connection.id}/"), {"path": "/"}).status_code == 403
    assert Client().post(target_path(personal, "new/"), document(personal), HTTP_AUTHORIZATION="Bearer " + personal.token).status_code == 403
    registration = queue(personal)
    assert client.post(target_path(personal, f"registrations/{registration.id}/")).status_code == 403
    outsider = Client()
    outsider.force_login(personal.outsider)
    assert outsider.get(target_path(personal, f"registrations/{registration.id}/")).status_code in {403, 404}
    assert outsider.get(target_path(personal, f"directories/{personal.connection.id}/")).status_code in {403, 404}
    personal.owner.is_active = False
    personal.owner.save(update_fields=["is_active"])
    assert personal.human.get(target_path(personal, f"registrations/{registration.id}/")).status_code == 302


def test_local_owner_must_remain_active_before_saving(personal):
    registration = queue(personal)
    assert complete(personal, registration).status_code == 200
    personal.owner.is_active = False
    personal.owner.save(update_fields=["is_active"])
    assert save(personal, registration).status_code in {302, 403}
    assert not StorageTarget.objects.exists()


@pytest.mark.parametrize("root", ["/", "/scratch/../secret", "/scratch//results", "/scratch/unsafe;command", "relative"])
def test_invalid_personal_roots_never_queue_work(personal, root):
    response = personal.human.post(target_path(personal, "new/"), document(personal, root_path=root))
    assert response.status_code == 200 and not ConnectorTask.objects.exists()
