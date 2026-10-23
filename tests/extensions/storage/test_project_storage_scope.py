"""Project-owned planning targets never widen user-owned HPC authority."""

from datetime import timedelta
from uuid import uuid4

from django.utils import timezone
import pytest

from compute_broker.control.auth import token_hash
from compute_broker.control.errors import BrokerError
from compute_broker.control.models import (AuditEvent, ClusterRegistration, ConnectorEnrollment,
    Membership, Project, ProjectCredential)
from compute_broker.extensions.storage import services
from compute_broker.extensions.storage.models import (ProjectStorageBinding, ProjectStorageBudget,
    StorageEstimateReservation, StorageTarget, StorageTargetRegistration)
from compute_broker.extensions.storage.planning import ROLES, plan_storage


def enroll(resources, *, project=None, user=None, alias="staging-root-0", **kwargs):
    return services.register_target(user or resources.owner, resources.connection, alias, "Project storage",
        project=project or resources.project, **kwargs)


def other_project(resources):
    project = Project.objects.create(owner=resources.owner, name="Another storage project")
    project.connections.add(resources.connection)
    return project


def make_personal(resources):
    resources.worker.connector_features = ["personal_setup_v1", "personal_folders_v1"]
    resources.worker.save(update_fields=["connector_features"])
    registration = ClusterRegistration.objects.create(owner=resources.owner, name="Personal test cluster",
        host="storage.invalid", remote_user="researcher", connection=resources.connection)
    ConnectorEnrollment.objects.create(registration=registration, worker=resources.worker, setup_revision=1,
        token_hash=token_hash("synthetic-project-scope-pairing"), consumed_at=timezone.now(),
        expires_at=timezone.now() + timedelta(minutes=5))


def queue(resources, *, project=None, submission_id=None):
    return services.queue_personal_target(resources.owner, resources.connection, "/scratch/researcher/project",
        "Dedicated project", ["work", "environments", "data", "results"], resources.connection.revision_sha256,
        submission_id or uuid4(), project=project or resources.project)


def complete(registration):
    task = registration.task
    task.state = "succeeded"
    task.result = {"ok": True, "path": registration.root, "parent": "/scratch/researcher",
        "writable": True, "created": False}
    task.save(update_fields=["state", "result"])


def test_new_enrollment_requires_explicit_project_and_project_admin(resources):
    with pytest.raises(TypeError):
        services.register_target(resources.owner, resources.connection, "staging-root-0", "Missing project")
    with pytest.raises(BrokerError) as denied:
        services.register_target(resources.owner, resources.connection, "staging-root-0", "Missing project", project=None)
    assert denied.value.code == "STORAGE_PROJECT_REQUIRED"
    project = Project.objects.create(owner=resources.outsider, name="Other administrator")
    project.connections.add(resources.connection)
    with pytest.raises(BrokerError) as denied:
        enroll(resources, project=project)
    assert denied.value.code == "SCOPE_DENIED"
    Membership.objects.create(project=project, user=resources.owner, roles=["viewer"])
    with pytest.raises(BrokerError) as denied:
        enroll(resources, project=project)
    assert denied.value.code == "SCOPE_DENIED"
    assert not StorageTarget.objects.exists()


@pytest.mark.parametrize("change", ["not_assigned", "disabled", "archived"])
def test_enrollment_rejects_unassigned_or_disabled_cluster(resources, change):
    if change == "not_assigned":
        resources.project.connections.remove(resources.connection)
    else:
        setattr(resources.connection, "enabled" if change == "disabled" else "archived", change == "archived")
        resources.connection.save()
    with pytest.raises(BrokerError) as denied:
        enroll(resources)
    assert denied.value.code == "STORAGE_TARGET_DENIED"
    assert not StorageTarget.objects.exists()


def test_project_admin_can_use_own_hpc_account_in_another_owners_project(resources):
    project = Project.objects.create(owner=resources.outsider, name="Shared research project")
    project.connections.add(resources.connection)
    Membership.objects.create(project=project, user=resources.owner, roles=["project_admin"])
    target = enroll(resources, project=project)
    assert target.owner_id == resources.connection.owner_id != project.owner_id
    assert target.project_id == project.id
    assert services.target_current(target)
    assert list(services.selected_targets(project, resources.connection)) == [target]
    binding = ProjectStorageBinding.objects.get(project=project, connection=resources.connection)
    assert all(getattr(binding, f"{role}_id") == target.id for role in ROLES)
    assert not ProjectStorageBudget.objects.exists()
    services.save_project_storage(resources.owner, project, resources.connection,
        {"bytes_budget": 100, "data": target, "retained_bytes_estimate": 1})
    reservation = services.reserve_estimate(project, target, uuid4(), 10, 1)
    assert reservation.project_id == project.id
    key = ProjectCredential.objects.create(project=project, issued_by=resources.owner, name="Own cluster planner",
        token_hash=token_hash("synthetic-scope-project-key"), scopes=["jobs:read"],
        expires_at=timezone.now() + timedelta(days=1))
    inventory = services.inventory(project, credential=key)
    assert inventory["targets"][0]["project_id"] == str(project.id)
    assert inventory["bindings"][0]["targets"]["data"] == str(target.id)
    assert target.root not in str(inventory)
    key.issued_by = resources.outsider
    key.save(update_fields=["issued_by"])
    assert services.inventory(project, credential=key)["targets"] == []


def test_project_admin_does_not_gain_another_users_cluster(resources):
    Membership.objects.create(project=resources.project, user=resources.outsider, roles=["project_admin"])
    with pytest.raises(BrokerError) as denied:
        enroll(resources, user=resources.outsider)
    assert denied.value.code == "STORAGE_TARGET_DENIED"


def test_same_published_root_is_registered_per_project_with_a_shared_capacity_pool(resources):
    first = enroll(resources)
    project = other_project(resources)
    second = enroll(resources, project=project, pool=first.pool)
    assert second.id != first.id and second.pool_id == first.pool_id
    assert second.root == first.root
    assert enroll(resources).id == first.id
    assert enroll(resources, project=project).id == second.id
    assert services.inventory(resources.project)["targets"][0]["id"] == str(first.id)
    assert services.inventory(project)["targets"][0]["id"] == str(second.id)


def test_foreign_project_targets_are_rejected_even_for_same_cluster_owner(resources):
    target = enroll(resources)
    project = other_project(resources)
    with pytest.raises(BrokerError) as denied:
        services.save_project_storage(resources.owner, project, resources.connection, {"data": target})
    assert denied.value.code == "STORAGE_TARGET_DENIED"
    ProjectStorageBinding.objects.create(project=project, connection=resources.connection, data=target)
    inventory = services.inventory(project)
    assert inventory["targets"] == [] and inventory["bindings"][0]["targets"]["data"] is None
    assert list(services.selected_targets(project, resources.connection)) == []
    with pytest.raises(BrokerError) as denied:
        services.reserve_estimate(project, target, uuid4(), 1, 1)
    assert denied.value.code == "STORAGE_TARGET_DENIED"


def test_data_role_supports_input_dataset_planning_without_filesystem_actions(resources):
    target = enroll(resources)
    result = plan_storage({"connection_id": str(resources.connection.id), "estimates": [
        {"role": "data", "target_id": str(target.id), "peak_bytes": 100, "peak_inodes": 1}]},
        services.inventory(resources.project))
    assert result["status"] == "unknown" and result["dry_run"] is True
    assert result["creates_reservations"] is False


def test_new_default_bindings_never_overwrite_existing_choices_or_budget(resources):
    first = enroll(resources)
    binding = ProjectStorageBinding.objects.get(project=resources.project)
    for role in ROLES:
        if role != "work":
            setattr(binding, role, None)
    binding.save()
    budget = ProjectStorageBudget.objects.create(project=resources.project, bytes_budget=123,
        inodes_budget=456, retained_bytes_estimate=12, retained_inodes_estimate=34,
        estimate_source="human_estimate", estimate_updated_at=timezone.now())
    before = {field: getattr(budget, field) for field in ("bytes_budget", "inodes_budget",
        "retained_bytes_estimate", "retained_inodes_estimate", "estimate_source", "estimate_updated_at")}
    resources.worker.profiles = [{**resources.profile, "storage_roots": [{"alias": "second", "path": "/approved/second",
        "roles": list(ROLES), "capabilities": {"visibility": "unknown", "formats": ["directory"], "quota_reporting": "unknown"}}]}]
    resources.worker.save(update_fields=["profiles"])
    second = enroll(resources, alias="second")
    binding.refresh_from_db();budget.refresh_from_db()
    assert binding.work_id == first.id
    assert all(getattr(binding, f"{role}_id") == second.id for role in ROLES if role != "work")
    assert all(getattr(budget, field) == value for field, value in before.items())


def test_legacy_four_role_target_stays_current_and_only_explicit_reenrollment_adds_data(resources):
    target = enroll(resources)
    target.roles = [role for role in ROLES if role != "data"]
    target.save(update_fields=["roles"])
    ProjectStorageBinding.objects.filter(project=resources.project).update(data=None)
    assert services.target_current(target)
    assert services.inventory(resources.project)["bindings"][0]["targets"]["data"] is None
    refreshed = enroll(resources)
    assert refreshed.id == target.id and "data" in refreshed.roles
    assert ProjectStorageBinding.objects.get(project=resources.project).data_id == target.id
    assert AuditEvent.objects.filter(project=resources.project, action="storage.target_roles_refreshed").count() == 1


def quarantine(target):
    StorageTarget.objects.filter(id=target.id).update(project=None)
    target.refresh_from_db()
    return target


def test_unassigned_legacy_target_is_hidden_until_explicit_owner_adoption(resources):
    target = quarantine(enroll(resources))
    assert not services.target_current(target)
    assert list(services.selected_targets(resources.project, resources.connection)) == []
    assert services.inventory(resources.project)["targets"] == []
    with pytest.raises(BrokerError) as denied:
        services.reserve_estimate(resources.project, target, uuid4(), 1, 1)
    assert denied.value.code == "STORAGE_TARGET_DENIED"
    root, pool = target.root, target.pool_id
    adopted = services.adopt_target(resources.owner, resources.project, target)
    assert adopted.project_id == resources.project.id
    assert adopted.root == root and adopted.pool_id == pool and services.target_current(adopted)
    assert services.adopt_target(resources.owner, resources.project, adopted).id == target.id
    assert AuditEvent.objects.filter(action="storage.target_adopted").count() == 1


@pytest.mark.parametrize("reference", ["binding", "held_estimate", "released_estimate"])
def test_adoption_never_moves_targets_with_another_projects_references(resources, reference):
    target = quarantine(enroll(resources))
    project = other_project(resources)
    if reference == "binding":
        ProjectStorageBinding.objects.create(project=project, connection=resources.connection, data=target)
    else:
        StorageEstimateReservation.objects.create(project=project, target=target, reference=uuid4(),
            bytes_estimate=10, inodes_estimate=1, state="released" if reference == "released_estimate" else "held")
    with pytest.raises(BrokerError) as denied:
        services.adopt_target(resources.owner, resources.project, target)
    assert denied.value.code == "STORAGE_TARGET_AMBIGUOUS"
    target.refresh_from_db()
    assert target.project_id is None


def test_adoption_requires_location_owner_project_admin_and_assigned_cluster(resources):
    target = quarantine(enroll(resources))
    Membership.objects.create(project=resources.project, user=resources.outsider, roles=["project_admin"])
    with pytest.raises(BrokerError) as denied:
        services.adopt_target(resources.outsider, resources.project, target)
    assert denied.value.code == "STORAGE_TARGET_DENIED"
    project = Project.objects.create(owner=resources.outsider, name="No membership")
    project.connections.add(resources.connection)
    with pytest.raises(BrokerError) as denied:
        services.adopt_target(resources.owner, project, target)
    assert denied.value.code == "SCOPE_DENIED"
    resources.project.connections.remove(resources.connection)
    with pytest.raises(BrokerError) as denied:
        services.adopt_target(resources.owner, resources.project, target)
    assert denied.value.code == "STORAGE_TARGET_DENIED"


def test_personal_registration_and_exact_receipt_belong_to_one_project(resources):
    make_personal(resources)
    registration = queue(resources)
    assert registration.project_id == resources.project.id
    assert registration.task.request == {"path": "/scratch/researcher", "name": "project"}
    assert queue(resources, submission_id=registration.id).id == registration.id
    with pytest.raises(BrokerError) as denied:
        queue(resources, project=other_project(resources), submission_id=registration.id)
    assert denied.value.code == "NOT_FOUND"
    complete(registration)
    target = services.finish_personal_target(resources.owner, registration, confirmed_root=registration.root)
    assert target.project_id == registration.project_id
    assert target.owner_id == resources.connection.owner_id
    assert ProjectStorageBinding.objects.get(project=resources.project).data_id == target.id
    assert services.target_current(target)


@pytest.mark.parametrize("change", ["connection_removed", "project_role_removed", "legacy_registration"])
def test_personal_finish_rechecks_project_authority_after_worker_receipt(resources, change):
    make_personal(resources)
    if change == "project_role_removed":
        project = Project.objects.create(owner=resources.outsider, name="Member-managed project")
        project.connections.add(resources.connection)
        membership = Membership.objects.create(project=project, user=resources.owner, roles=["project_admin"])
    else:
        project = resources.project
    registration = queue(resources, project=project)
    complete(registration)
    if change == "connection_removed":
        project.connections.remove(resources.connection)
    elif change == "project_role_removed":
        membership.delete()
    else:
        registration.project = None
        registration.save(update_fields=["project"])
    with pytest.raises(BrokerError) as denied:
        services.finish_personal_target(resources.owner, registration, confirmed_root=registration.root)
    assert denied.value.code in {"STORAGE_TARGET_DENIED", "SCOPE_DENIED", "STORAGE_PROJECT_REQUIRED"}
    assert not StorageTarget.objects.exists()


def test_detached_personal_registration_is_rejected_before_connector_locks(resources, monkeypatch):
    from compute_broker.control import connector_setup
    make_personal(resources)
    registration = queue(resources)
    complete(registration)
    resources.project.connections.remove(resources.connection)
    def forbidden_lock(*args, **kwargs):
        raise AssertionError("An unassigned project must be rejected before connector locking.")
    monkeypatch.setattr(connector_setup, "locked_connection", forbidden_lock)
    with pytest.raises(BrokerError) as denied:
        services.finish_personal_target(resources.owner, registration, confirmed_root=registration.root)
    assert denied.value.code == "STORAGE_TARGET_DENIED"
    assert not StorageTarget.objects.exists()


@pytest.mark.parametrize("operation", ["queue", "finish"])
def test_assignment_removed_before_connector_lock_never_takes_an_additional_project_lock(resources, monkeypatch, operation):
    from compute_broker.control import connector_setup
    make_personal(resources)
    registration = queue(resources) if operation == "finish" else None
    if registration:
        complete(registration)
    original_lock = connector_setup.locked_connection
    manager = Project.objects
    original_project_lock = manager.select_for_update
    locks = []
    def record_project_lock(*args, **kwargs):
        locks.append(True)
        return original_project_lock(*args, **kwargs)
    monkeypatch.setattr(manager, "select_for_update", record_project_lock)
    def detach_then_lock(*args, **kwargs):
        resources.project.connections.remove(resources.connection)
        return original_lock(*args, **kwargs)
    monkeypatch.setattr(connector_setup, "locked_connection", detach_then_lock)
    with pytest.raises(BrokerError) as denied:
        if registration:
            services.finish_personal_target(resources.owner, registration, confirmed_root=registration.root)
        else:
            queue(resources)
    assert denied.value.code == "STORAGE_TARGET_DENIED"
    # The core helper makes one ordered project-lock query before Connection.
    # Extension code must not add a second project lock after Connection when
    # the project is absent from that core query because it was detached.
    assert len(locks) == 1
    assert not StorageTarget.objects.exists()
    assert StorageTargetRegistration.objects.count() == (1 if registration else 0)


def test_adoption_updates_personal_registration_without_changing_receipt_or_budget(resources):
    make_personal(resources)
    registration = queue(resources)
    complete(registration)
    target = services.finish_personal_target(resources.owner, registration, confirmed_root=registration.root)
    quarantine(target)
    StorageTargetRegistration.objects.filter(id=registration.id).update(project=None)
    original_receipt = dict(registration.task.result)
    adopted = services.adopt_target(resources.owner, resources.project, target)
    registration.refresh_from_db()
    assert registration.project_id == resources.project.id and services.target_current(adopted)
    assert registration.task.result == original_receipt
    assert not ProjectStorageBudget.objects.exists()
