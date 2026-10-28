"""Storage extension authorization, project boundaries and cached planning."""
from test_support.extensions_storage_test_storage_control import enroll

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from compute_relay.control.auth import token_hash
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Connection, Job, Project, ProjectCredential, User, WorkerIdentity
from compute_relay.extensions.storage import services
from compute_relay.extensions.storage.models import ProjectStorageBinding, ProjectStorageBudget, StorageCapacityPool, StorageEstimateReservation, StorageTarget
from compute_relay.extensions.storage.planning import ROLES




def select(resources, target, **changes):
    return services.save_project_storage(resources.owner, resources.project, resources.connection,
        {"bytes_budget": 10000, "inodes_budget": 10000, "retained_bytes_estimate": 100, "retained_inodes_estimate": 10,
         **{role: target for role in ROLES}, **changes})


def headers(resources):
    return {"HTTP_AUTHORIZATION": "Bearer " + resources.token}


def plan(resources, target, **changes):
    return {"connection_id": str(resources.connection.id), "estimates": [{"role": "work", "target_id": str(target.id),
                                                                         "peak_bytes": 100, "peak_inodes": 10, **changes}]}


def observation(*, now=None, filesystem_id="volume:test", available_bytes=8000, available_inodes=8000):
    return {"schema_version": "1.0", "source": "site_probe", "observed_at": (now or timezone.now()).isoformat(),
            "status": "available", "filesystem_id": filesystem_id, "identity_verified": True,
            "total_bytes": 10000, "available_bytes": available_bytes, "total_inodes": 10000,
            "available_inodes": available_inodes, "quota": {"status": "reported", "available_bytes": 9000, "available_inodes": 9000}}


def test_human_enrollment_selects_only_existing_protected_roots_and_writes_no_job(resources):
    client = Client()
    client.force_login(resources.owner)
    response = client.get(f"/projects/{resources.project.id}/storage/targets/new/?connection={resources.connection.id}")
    assert response.status_code == 200 and b"/approved/project-space" in response.content
    response = client.post(f"/projects/{resources.project.id}/storage/targets/new/", {"name": "Data", "connection": resources.connection.id,
                                                     "root_alias": "/arbitrary/path"})
    assert response.status_code == 200 and not StorageTarget.objects.exists()
    response = client.post(f"/projects/{resources.project.id}/storage/targets/new/", {"name": "Data", "connection": resources.connection.id,
                                                     "root_alias": "staging-root-0"})
    assert response.status_code == 302
    assert StorageTarget.objects.get().root == "/approved/project-space"
    assert StorageCapacityPool.objects.get().observation == {}
    assert not Job.objects.exists()


def test_human_storage_writes_require_session_csrf_and_owner_scope(resources):
    document = {"name": "Data", "connection": resources.connection.id, "root_alias": "staging-root-0"}
    assert Client().post(f"/projects/{resources.project.id}/storage/targets/new/", document, **headers(resources)).status_code == 403
    outsider = Client()
    outsider.force_login(resources.outsider)
    response = outsider.post(f"/projects/{resources.project.id}/storage/targets/new/", document)
    assert response.status_code == 403 and not StorageTarget.objects.exists()
    assert outsider.get(f"/projects/{resources.project.id}/storage/").status_code == 403
    human = Client(enforce_csrf_checks=True)
    human.force_login(resources.owner)
    assert human.post(f"/projects/{resources.project.id}/storage/targets/new/", document).status_code == 403
    human.get(f"/projects/{resources.project.id}/storage/targets/new/?connection={resources.connection.id}")
    response = human.post(f"/projects/{resources.project.id}/storage/targets/new/", document, HTTP_X_CSRFTOKEN=human.cookies["csrftoken"].value)
    assert response.status_code == 302


def test_project_form_saves_manual_estimate_and_scoped_role_selectors(resources):
    target = enroll(resources)
    client = Client()
    client.force_login(resources.owner)
    response = client.post(f"/projects/{resources.project.id}/storage/", {"connection_id": resources.connection.id,
        "budget_gib": "2", "inodes_budget": "1000", "retained_gib_estimate": "0.5", "retained_inodes_estimate": "20",
        **{role: target.id for role in ROLES}})
    assert response.status_code == 302, response.content
    budget = ProjectStorageBudget.objects.get(project=resources.project)
    assert budget.bytes_budget == 2 * 1024**3 and budget.retained_bytes_estimate == 1024**3 // 2
    assert budget.estimate_source == "human_estimate" and budget.estimate_updated_at is not None
    assert ProjectStorageBinding.objects.get().results_id == target.id
    page = client.get(f"/projects/{resources.project.id}/storage/")
    assert b"advisory estimates" in page.content and b"not connected" in page.content


def test_project_storage_update_requires_csrf(resources):
    client = Client(enforce_csrf_checks=True)
    client.force_login(resources.owner)
    assert client.post(f"/projects/{resources.project.id}/storage/", {"connection_id": resources.connection.id}).status_code == 403
    assert not ProjectStorageBudget.objects.exists()


def test_inventory_and_plan_are_read_only_scoped_and_explicit_about_unknown_capacity(resources):
    target = enroll(resources)
    select(resources, target)
    client = Client()
    response = client.get(f"/api/v1/projects/{resources.project.id}/storage", **headers(resources))
    assert response.status_code == 200
    payload = response.json()
    assert payload["probe_wired"] is True and payload["advisory_only"] is True
    assert payload["pools"][0]["probe_enabled"] is False
    assert payload["pools"][0]["measurement_status"] == "not_observed"
    assert "/approved/project-space" not in response.content.decode()
    assert "root" not in payload["targets"][0]
    response = client.post(f"/api/v1/projects/{resources.project.id}/storage/plan", json.dumps(plan(resources, target)),
                           content_type="application/json", **headers(resources))
    assert response.status_code == 200, response.content
    payload = response.json()
    assert payload["status"] == "unknown" and payload["creates_reservations"] is False
    assert payload["enforced_at_dispatch"] is False and payload["dry_run"] is True
    assert not StorageEstimateReservation.objects.exists() and not Job.objects.exists()


def test_api_rejects_foreign_project_expired_and_wrong_scope_credentials(resources):
    foreign = Project.objects.create(owner=resources.outsider, name="other project")
    client = Client()
    assert client.get(f"/api/v1/projects/{foreign.id}/storage", **headers(resources)).status_code == 404
    resources.credential.scopes = ["jobs:write"]
    resources.credential.save(update_fields=["scopes"])
    assert client.get(f"/api/v1/projects/{resources.project.id}/storage", **headers(resources)).status_code == 403
    resources.credential.expires_at = timezone.now() - timedelta(seconds=1)
    resources.credential.save(update_fields=["expires_at"])
    assert client.get(f"/api/v1/projects/{resources.project.id}/storage", **headers(resources)).status_code == 401


def test_key_cluster_deny_hides_targets_and_denies_planning(resources):
    target = enroll(resources)
    select(resources, target)
    resources.credential.allowed_connection_ids = []
    resources.credential.save(update_fields=["allowed_connection_ids"])
    client = Client()
    payload = client.get(f"/api/v1/projects/{resources.project.id}/storage", **headers(resources)).json()
    assert payload["bindings"] == payload["targets"] == payload["pools"] == []
    response = client.post(f"/api/v1/projects/{resources.project.id}/storage/plan", json.dumps(plan(resources, target)),
                           content_type="application/json", **headers(resources))
    assert response.status_code == 403 and response.json()["error"]["code"] == "CONNECTION_DENIED"


def test_foreign_targets_cannot_be_selected_or_dry_run(resources):
    target = enroll(resources)
    select(resources, target)
    other = Project.objects.create(owner=resources.outsider, name="foreign storage")
    other.connections.add(resources.connection)
    with pytest.raises(BrokerError) as denied:
        services.save_project_storage(resources.outsider, other, resources.connection,
            {"bytes_budget": 1000, "retained_bytes_estimate": 0, "retained_inodes_estimate": 0, "work": target})
    assert denied.value.code == "STORAGE_TARGET_DENIED"
    document = plan(resources, target)
    document["estimates"][0]["target_id"] = str(uuid4())
    with pytest.raises(BrokerError) as denied:
        services.plan_for_credential(resources.credential, document)
    assert denied.value.code == "STORAGE_TARGET_DENIED"


def test_profile_change_invalidates_new_storage_plans_without_changing_estimates(resources):
    target = enroll(resources)
    select(resources, target)
    resources.connection.revision_sha256 = "b" * 64
    resources.connection.save(update_fields=["revision_sha256"])
    payload = services.inventory(resources.project, credential=resources.credential)
    assert payload["targets"] == []
    assert payload["budget"]["retained_bytes_estimate"] == 100
    with pytest.raises(BrokerError) as denied:
        services.plan_for_credential(resources.credential, plan(resources, target))
    assert denied.value.code == "STORAGE_TARGET_DENIED"


def test_observation_hook_is_worker_scoped_fresh_and_never_claims_unpinned_identity(resources):
    target = enroll(resources)
    select(resources, target)
    other_worker = WorkerIdentity.objects.create(owner=resources.outsider, name="outsider", token_hash=token_hash("other-worker"))
    with pytest.raises(BrokerError) as denied:
        services.record_observation(other_worker, target.pool_id, observation())
    assert denied.value.status == 404
    value = observation()
    services.record_observation(resources.worker, target.pool_id, value)
    target.pool.refresh_from_db()
    assert target.pool.observation["identity_verified"] is False
    assert target.pool.health_state == "unknown"
    with pytest.raises(BrokerError) as denied:
        services.record_observation(resources.worker, target.pool_id, value)
    assert denied.value.code == "STALE_OBSERVATION"


def test_advisory_reservations_are_idempotent_and_share_project_budget(resources):
    target = enroll(resources)
    select(resources, target, bytes_budget=1000, retained_bytes_estimate=100)
    reference = uuid4()
    first = services.reserve_estimate(resources.project, target, reference, 600, 1)
    assert services.reserve_estimate(resources.project, target, reference, 600, 1).id == first.id
    with pytest.raises(BrokerError) as conflict:
        services.reserve_estimate(resources.project, target, reference, 601, 1)
    assert conflict.value.code == "STORAGE_RESERVATION_CONFLICT"
    with pytest.raises(BrokerError) as exceeded:
        services.reserve_estimate(resources.project, target, uuid4(), 301, 1)
    assert exceeded.value.code == "STORAGE_BUDGET_EXCEEDED"
    assert services.inventory(resources.project)["budget"]["reserved_bytes_estimate"] == 600
    services.release_estimate(resources.project, reference)
    services.release_estimate(resources.project, reference)
    assert services.inventory(resources.project)["budget"]["reserved_bytes_estimate"] == 0
    assert services.reserve_estimate(resources.project, target, reference, 600, 1).state == "released"


def test_internal_reservation_cannot_cross_project_or_target_scope(resources):
    target = enroll(resources)
    select(resources, target)
    project = Project.objects.create(owner=resources.outsider, name="foreign")
    project.connections.add(resources.connection)
    with pytest.raises(BrokerError) as denied:
        services.reserve_estimate(project, target, uuid4(), 100, 1)
    assert denied.value.code == "STORAGE_TARGET_DENIED"
    reference = uuid4()
    services.reserve_estimate(resources.project, target, reference, 100, 1)
    assert services.release_estimate(project, reference) is None
    assert StorageEstimateReservation.objects.get().state == "held"


def test_plan_body_limit_and_unknown_path_fields_are_rejected(resources):
    target = enroll(resources)
    select(resources, target)
    client = Client()
    document = plan(resources, target)
    document["estimates"][0]["path"] = "/private/elsewhere"
    response = client.post(f"/api/v1/projects/{resources.project.id}/storage/plan", json.dumps(document),
                           content_type="application/json", **headers(resources))
    assert response.status_code == 400
    response = client.post(f"/api/v1/projects/{resources.project.id}/storage/plan", json.dumps({"large": "x" * 16384}),
                           content_type="application/json", **headers(resources))
    assert response.status_code == 413


def pinned_target(resources):
    """Synthetic protected metadata for the future separate-root publishing hook."""
    from compute_relay.control.services import digest
    from compute_relay.extensions.storage.planning import default_health_policy
    root = {"alias": "data", "path": "/approved/data", "roles": list(ROLES), "filesystem_id": "volume:test"}
    profile = {**resources.profile, "storage_roots": [root]}
    profile["revision_sha256"] = digest({key: value for key, value in profile.items() if key != "revision_sha256"})
    resources.worker.profiles = [profile]
    resources.worker.save(update_fields=["profiles"])
    resources.connection.configuration = {**resources.connection.configuration, "credential_profile_revision_sha256": profile["revision_sha256"]}
    resources.connection.revision_sha256 = digest(resources.connection.configuration)
    resources.connection.save(update_fields=["configuration", "revision_sha256"])
    target = services.register_target(resources.owner, resources.connection, "data", "Pinned data", project=resources.project)
    target.pool.health_policy = {**default_health_policy(), **{f"{severity}_floor_{unit}": 0
        for severity in ("warning", "stop", "emergency", "recovery") for unit in ("bytes", "inodes")}}
    target.pool.save(update_fields=["health_policy"])
    select(resources, target)
    return target


def test_cached_physical_and_quota_observations_drive_http_planning(resources):
    target = pinned_target(resources)
    services.record_observation(resources.worker, target.pool_id, observation())
    client = Client()
    url = f"/api/v1/projects/{resources.project.id}/storage/plan"
    response = client.post(url, json.dumps(plan(resources, target)), content_type="application/json", **headers(resources))
    assert response.status_code == 200 and response.json()["status"] == "ready"
    value = observation()
    value["quota"]["available_bytes"] = 50
    services.record_observation(resources.worker, target.pool_id, value)
    response = client.post(url, json.dumps(plan(resources, target)), content_type="application/json", **headers(resources))
    assert response.json()["status"] == "blocked"
    assert any("reported_quota_bytes_insufficient" in reason for reason in response.json()["reasons"])
    payload = client.get(f"/api/v1/projects/{resources.project.id}/storage", **headers(resources)).json()
    assert payload["pools"][0]["measurement_status"] == "cached"
    assert payload["pools"][0]["health"]["observed_at"] == value["observed_at"]
    assert payload["pools"][0]["observation"]["quota"]["available_bytes"] == 50


def test_cached_capacity_expiration_and_mount_identity_mismatch_do_not_report_ready(resources):
    target = pinned_target(resources)
    value = observation(now=timezone.now() - timedelta(seconds=121))
    services.record_observation(resources.worker, target.pool_id, value)
    result = services.plan_for_credential(resources.credential, plan(resources, target))
    assert result["status"] == "unknown" and result["pools"][0]["health"]["state"] == "stale"
    services.record_observation(resources.worker, target.pool_id, observation(filesystem_id="different:filesystem"))
    result = services.plan_for_credential(resources.credential, plan(resources, target))
    assert result["status"] == "unknown" and result["pools"][0]["health"]["state"] == "unavailable"
    target.pool.refresh_from_db()
    assert target.pool.observation["error_code"] == "FILESYSTEM_IDENTITY_MISMATCH"


def test_inode_exhaustion_blocks_plan_even_when_bytes_and_project_budget_fit(resources):
    target = pinned_target(resources)
    services.record_observation(resources.worker, target.pool_id, observation(available_inodes=10))
    result = services.plan_for_credential(resources.credential, plan(resources, target))
    assert result["status"] == "blocked"
    assert result["pools"][0]["health"]["state"] == "emergency"


def test_global_pool_plans_include_other_projects_pending_growth_without_double_counting_data(resources):
    target = pinned_target(resources)
    services.record_observation(resources.worker, target.pool_id, observation())
    other = Project.objects.create(owner=resources.owner, name="Other project on the shared NAS")
    other.connections.add(resources.connection)
    other_target = services.register_target(resources.owner, resources.connection, "data", "Other project data",
        project=other, pool=target.pool)
    services.save_project_storage(resources.owner, other, resources.connection,
        {"bytes_budget": 10000, "inodes_budget": 10000, "retained_bytes_estimate": 0, "retained_inodes_estimate": 0, "work": other_target})
    services.reserve_estimate(other, other_target, uuid4(), 7500, 1)
    result = services.plan_for_credential(resources.credential, plan(resources, target))
    assert result["status"] == "blocked"
    assert result["pools"][0]["headroom_bytes_estimate"] == 8000 - 7500 - 100 - 500
    assert services.inventory(resources.project)["budget"]["reserved_bytes_estimate"] == 0
