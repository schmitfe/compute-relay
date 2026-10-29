"""Human management boundaries, one-time keys, and shared project authority.

All identities, credentials, objects, and fake cluster profiles are synthetic.
No test opens a host SSH key, invokes a cluster, or talks to the portal process.
"""
from test_support.unit_test_management import agent_get, discovery_report, form_post, human, issued, key_data, management, prepared, queued_discovery

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.apps import apps
from django.contrib.sessions.models import Session
from django.db import transaction
from django.test import Client
from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.auth import new_token, token_hash
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import (
    Artifact, Attempt, Connection, Environment, ExecutionRequest, Grant,
    Job, Project, ProjectCredential, User, WorkerIdentity,
)
from compute_relay.control.storage import store_blob, store_uploaded_blob


SCOPES = ["objects:write", "jobs:read", "jobs:write", "jobs:execute", "jobs:cancel", "artifacts:read"]








def project_data(context, **changes):
    data = {"name": "updated managed project", "mode": "approval_required", "cpu_hours": "2", "gpu_hours": "1",
            "max_cpus": "2", "max_gpus": "1", "memory_mib": "512", "wall_minutes": "10",
            "max_active_jobs": "2", "max_total_jobs": "1000", "expires_at": (timezone.now() + timedelta(days=3)).isoformat(),
            "connections": [str(c.id) for c in context.connections], "environments": [str(context.environment.id)],
            "allowed_gpu_types": ["a100"]}
    data.update(changes)
    return data






def assert_not_persisted(secret):
    for model in apps.get_models():
        for record in model._default_manager.values():
            assert secret not in json.dumps(record, default=str), model._meta.label
    for session in Session.objects.all():
        assert secret not in json.dumps(session.get_decoded(), default=str)






def execution(context, job):
    with transaction.atomic():
        result, _ = services.request_execution(job, context.credential)
    return ExecutionRequest.objects.get(id=result["id"])


def test_management_mutations_require_human_session_and_csrf(management):
    context = management
    paths = ["/projects/new/", f"/projects/{context.project.id}/", f"/projects/{context.project.id}/keys/new/",
             f"/projects/{context.project.id}/keys/{context.credential.id}/rotate/",
             f"/projects/{context.project.id}/keys/{context.credential.id}/revoke/",
             f"/projects/{context.project.id}/keys/{context.credential.id}/edit/",
             "/clusters/new/", f"/clusters/{context.connections[0].id}/discover/",
             f"/clusters/{context.connections[0].id}/toggle/", f"/clusters/{context.connections[0].id}/environments/new/"]
    before = (Project.objects.count(), ProjectCredential.objects.count(), Connection.objects.count())
    for path in paths:
        session = human(context.owner)
        assert session.post(path, {}).status_code == 403
        anonymous = Client(enforce_csrf_checks=True)
        anonymous.get("/login/")
        response = anonymous.post(path, {"submission_id": str(uuid4())},
                                  HTTP_AUTHORIZATION="Bearer " + context.token,
                                  HTTP_X_CSRFTOKEN=anonymous.cookies["csrftoken"].value)
        assert response.status_code in {302, 401, 403}
    assert (Project.objects.count(), ProjectCredential.objects.count(), Connection.objects.count()) == before
    context.credential.refresh_from_db()
    assert context.credential.revoked is False


def test_other_owner_cannot_manage_project_keys_or_cluster(management):
    context = management
    outsider = human(context.outsider)
    paths = [f"/projects/{context.project.id}/", f"/projects/{context.project.id}/keys/new/",
             f"/projects/{context.project.id}/keys/{context.credential.id}/edit/",
             f"/clusters/{context.connections[0].id}/"]
    for path in paths:
        response = outsider.get(path)
        assert response.status_code in {403, 404}
        assert context.project.name.encode() not in response.content
    for action in ("revoke", "rotate"):
        response = form_post(outsider, f"/projects/{context.project.id}/keys/{context.credential.id}/{action}/",
                             {"submission_id": str(uuid4())})
        assert response.status_code in {403, 404}
    context.credential.refresh_from_db()
    assert not context.credential.revoked


def test_agent_job_name_is_preserved_in_api_and_review_attribution(management):
    context = management
    job = prepared(context, name="codex-protein-folding-run-17")
    detail = agent_get(context.token, f"/api/v1/jobs/{job.id}")
    assert detail.status_code == 200
    assert detail.json()["request"]["name"] == "codex-protein-folding-run-17"
    assert detail.json()["name"] == "codex-protein-folding-run-17"
    assert detail.json()["project_id"] == str(context.project.id)
    assert detail.json()["project_name"] == context.project.name
    assert detail.json()["owner"] == {"id": str(context.owner.id), "username": context.owner.username}
    assert detail.json()["submitted_using"] == {"id": str(context.credential.id), "name": context.credential.name}
    assert job.credential_id == context.credential.id
    page = human(context.owner).get(f"/review/{job.id}/")
    assert page.status_code == 200
    for label in (job.request["name"], context.project.name, context.owner.username, context.credential.name):
        assert label.encode() in page.content


def test_dashboard_attributes_jobs_to_project_owner_and_key_without_foreign_rows(management):
    context = management
    job = prepared(context, name="claude-analysis-named-job")
    foreign_project = Project.objects.create(owner=context.outsider, name="private-outsider-project")
    foreign_job = Job.objects.create(project=foreign_project, request={"name": "private-outsider-job"})
    page = human(context.owner).get("/")
    assert page.status_code == 200
    assert job.id in {entry.id for entry in page.context["jobs"]}
    assert foreign_job.id not in {entry.id for entry in page.context["jobs"]}
    for label in (job.request["name"], context.project.name, context.owner.username, context.credential.name):
        assert label.encode() in page.content
    assert foreign_project.name.encode() not in page.content
    assert foreign_job.request["name"].encode() not in page.content


def test_project_jobs_list_is_scoped_even_across_same_owners_projects(management):
    context = management
    job = prepared(context, name="project-specific-training-job")
    another_project = Project.objects.create(owner=context.owner, name="another-owned-project")
    another_job = Job.objects.create(project=another_project, request={"name": "other-project-only-job"})
    request = execution(context, job)
    request.state = "rejected"
    request.save(update_fields=["state"])
    page = human(context.owner).get(f"/projects/{context.project.id}/")
    assert page.status_code == 200
    assert {entry.id for entry in page.context["jobs"]} == {job.id}
    for label in (job.request["name"], context.owner.username, context.credential.name, "Rejected"):
        assert label.encode() in page.content
    assert another_job.request["name"].encode() not in page.content
    assert another_project.name.encode() not in page.content








@pytest.mark.parametrize("changes", [
    {"interpreter": "python3 -c bad"}, {"interpreter": "/usr/bin/../bad"},
    {"modules": "python/3.12; touch /tmp/bad"}, {"modules": "python/$(whoami)"},
    {"module_init": "/home/user/untrusted.sh"},
])
def test_environment_form_rejects_command_fragments_and_untrusted_hooks(management, changes):
    from compute_relay.control.forms import EnvironmentForm
    context = management
    data = {"name": "module Python", "connection": str(context.connections[0].id), "kind": "modules",
            "interpreter": "python3", "modules": "python/3.12", "module_init": "/etc/profile.d/modules.sh"}
    data.update(changes)
    assert not EnvironmentForm(data, user=context.owner).is_valid()


def test_new_key_is_shown_once_and_never_saved_as_plaintext(management):
    context = management
    owner = human(context.owner)
    path = f"/projects/{context.project.id}/keys/new/"
    data = key_data(context)
    secret, key = issued(form_post(owner, path, data))
    assert key.project_id == context.project.id
    assert_not_persisted(secret)
    for page in (path, f"/projects/{context.project.id}/", "/"):
        response = owner.get(page)
        assert response.status_code == 200
        assert secret.encode() not in response.content
    replay = form_post(owner, path, data)
    assert replay.status_code == 409
    assert secret.encode() not in replay.content
    assert ProjectCredential.objects.filter(project=context.project).count() == 2
    assert_not_persisted(secret)


def test_multiple_keys_use_existing_project_grant_and_same_balance(management):
    context = management
    Grant.objects.filter(id=context.grant.id).update(spent_cpu=120, reserved_cpu=60)
    original = services.allowance(context.project)
    owner = human(context.owner)
    key_ids = []
    for name in ("Codex test", "Claude test"):
        secret, key = issued(form_post(owner, f"/projects/{context.project.id}/keys/new/", key_data(context, name=name)))
        key_ids.append(key.id)
        observed = agent_get(secret, f"/api/v1/projects/{context.project.id}/allowance").json()
        assert observed.pop("compute")["per_job_limits"]["cpu_cores"] == 2
        assert observed == original
    assert len(set(key_ids)) == 2
    assert Grant.objects.filter(project=context.project).count() == 1
    assert Grant.objects.get(project=context.project).id == context.grant.id
    assert services.allowance(context.project) == original


def test_owner_creates_a_project_with_explicit_limits_and_cluster_access(management):
    context = management
    response = form_post(human(context.owner), "/projects/new/", project_data(context, name="new managed project"))
    assert response.status_code in {200, 302}
    project = Project.objects.get(name="new managed project", owner=context.owner)
    grant = Grant.objects.get(project=project)
    assert grant.policy["project_id"] == str(project.id)
    assert grant.policy["allowance"] == {"cpu_core_seconds": 7200, "gpu_device_seconds": 3600}
    assert grant.spent_cpu == grant.reserved_cpu == grant.spent_gpu == grant.reserved_gpu == 0
    assert set(project.connections.values_list("id", flat=True)) == {c.id for c in context.connections}
    assert not ProjectCredential.objects.filter(project=project).exists()


def test_rotation_revokes_old_key_and_preserves_scopes_expiry_and_restrictions(management):
    context = management
    context.credential.scopes = ["jobs:read"]
    context.credential.allowed_connection_ids = [str(context.connections[0].id)]
    context.credential.execution_mode = "approval_required"
    context.credential.save()
    owner = human(context.owner)
    path = f"/projects/{context.project.id}/keys/{context.credential.id}/rotate/"
    data = {"submission_id": str(uuid4())}
    secret, replacement = issued(form_post(owner, path, data))
    context.credential.refresh_from_db()
    replacement.refresh_from_db()
    assert context.credential.revoked
    assert replacement.id != context.credential.id
    assert replacement.scopes == context.credential.scopes
    assert replacement.allowed_connection_ids == context.credential.allowed_connection_ids
    assert replacement.expires_at == context.credential.expires_at
    assert replacement.execution_mode == "approval_required"
    assert agent_get(context.token, "/api/v1/connections").status_code == 401
    assert agent_get(secret, "/api/v1/connections").status_code == 200
    replay = form_post(owner, path, data)
    assert replay.status_code == 409
    assert ProjectCredential.objects.filter(project=context.project).count() == 2
    assert secret.encode() not in owner.get(f"/projects/{context.project.id}/").content
    assert_not_persisted(secret)


@pytest.mark.parametrize("mode", ["inherit", "approval_required"])
def test_key_creation_and_access_editor_store_explicit_execution_confirmation_mode(management, mode):
    context = management
    owner = human(context.owner)
    _, key = issued(form_post(owner, f"/projects/{context.project.id}/keys/new/", key_data(context, execution_mode=mode)))
    assert key.execution_mode == mode
    edit = owner.get(f"/projects/{context.project.id}/keys/{key.id}/edit/")
    assert edit.status_code == 200 and edit.context["form"].initial["execution_mode"] == mode
    other_mode = "approval_required" if mode != "approval_required" else "inherit"
    assert form_post(owner, f"/projects/{context.project.id}/keys/{key.id}/edit/",
                     key_data(context, execution_mode=other_mode)).status_code == 302
    key.refresh_from_db()
    assert key.execution_mode == other_mode


def test_review_key_forces_approval_on_budgeted_project_and_rejection_consumes_no_lifetime_slot(management):
    context = management
    context.grant.policy = {**context.grant.policy, "mode": "budgeted", "max_total_jobs": 1}
    context.grant.save(update_fields=["policy"])
    _, context.credential = issued(form_post(human(context.owner), f"/projects/{context.project.id}/keys/new/",
                                            key_data(context, execution_mode="approval_required")))
    job = prepared(context)
    request = execution(context, job)
    assert request.state == "awaiting_approval"
    assert not Attempt.objects.filter(job=job).exists()
    assert services.allowance(context.project)["jobs"]["admitted"] == 0
    with transaction.atomic():
        services.decide(job, context.owner, {"execution_request_id": str(request.id),
                                           "manifest_sha256": job.manifest_sha256, "approved": False})
    assert services.allowance(context.project)["jobs"]["remaining"] == 1
    next_request = execution(context, job)
    with transaction.atomic():
        services.decide(job, context.owner, {"execution_request_id": str(next_request.id),
                                           "manifest_sha256": job.manifest_sha256, "approved": True})
    assert Attempt.objects.filter(job=job).count() == 1
    assert services.allowance(context.project)["jobs"]["admitted"] == 1
    assert services.allowance(context.project)["jobs"]["remaining"] == 0


def test_inheriting_key_cannot_bypass_project_required_review(management):
    context = management
    context.credential.execution_mode = "inherit"
    context.credential.save(update_fields=["execution_mode"])
    job = prepared(context)
    assert execution(context, job).state == "awaiting_approval"
    assert not Attempt.objects.filter(job=job).exists()


def test_key_edit_requiring_review_blocks_an_automatic_attempt_before_dispatch(management):
    context = management
    context.grant.policy = {**context.grant.policy, "mode": "budgeted"}
    context.grant.save(update_fields=["policy"])
    job = prepared(context)
    request = execution(context, job)
    assert request.state == "authorized"
    attempt = Attempt.objects.get(execution_request=request)
    response = form_post(human(context.owner), f"/projects/{context.project.id}/keys/{context.credential.id}/edit/",
                         key_data(context, execution_mode="approval_required"))
    assert response.status_code == 302
    lease = services.lease_work(context.worker)["lease"]
    with pytest.raises(BrokerError) as denied:
        services.dispatch(context.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
                                                      "manifest_sha256": job.manifest_sha256,
                                                      "connection_revision_sha256": context.connections[0].revision_sha256})
    assert denied.value.status == 403
    attempt.refresh_from_db()
    assert attempt.dispatch_started is False


def test_project_form_zero_lifetime_limit_blocks_first_authorization(management):
    context = management
    response = form_post(human(context.owner), f"/projects/{context.project.id}/", project_data(context, max_total_jobs="0"))
    assert response.status_code == 302
    context.grant.refresh_from_db()
    assert context.grant.policy["max_total_jobs"] == 0
    assert services.allowance(context.project)["jobs"]["remaining"] == 0
    job = prepared(context)
    request = execution(context, job)
    with pytest.raises(BrokerError) as limit:
        with transaction.atomic():
            services.decide(job, context.owner, {"execution_request_id": str(request.id),
                                               "manifest_sha256": job.manifest_sha256, "approved": True})
    assert limit.value.code == "TOTAL_JOB_LIMIT"
    assert not Attempt.objects.filter(job=job).exists()


def test_project_form_cannot_reduce_lifetime_limit_below_durable_authorizations(management):
    context = management
    job = prepared(context)
    request = execution(context, job)
    with transaction.atomic():
        services.decide(job, context.owner, {"execution_request_id": str(request.id),
                                           "manifest_sha256": job.manifest_sha256, "approved": True})
    attempt = Attempt.objects.get(job=job)
    attempt.state = "cancelled"
    attempt.accounting_state = "settled"
    attempt.save(update_fields=["state", "accounting_state"])
    old_policy = context.grant.policy.copy()
    response = form_post(human(context.owner), f"/projects/{context.project.id}/", project_data(context, max_total_jobs="0"))
    assert response.status_code == 200
    assert "submission limit" in response.context["error"]
    context.grant.refresh_from_db()
    assert context.grant.policy == old_policy
    assert services.allowance(context.project)["jobs"]["admitted"] == 1


def test_failed_rotation_does_not_revoke_the_only_valid_key(management):
    context = management
    owner = human(context.owner)
    original_save = ProjectCredential.save

    def fail_new_credential(credential, *args, **kwargs):
        if credential._state.adding:
            raise RuntimeError("synthetic creation failure")
        return original_save(credential, *args, **kwargs)

    with patch.object(ProjectCredential, "save", new=fail_new_credential):
        with pytest.raises(RuntimeError, match="synthetic creation failure"):
            form_post(owner, f"/projects/{context.project.id}/keys/{context.credential.id}/rotate/",
                      {"submission_id": str(uuid4())})
    context.credential.refresh_from_db()
    assert context.credential.revoked is False
    assert ProjectCredential.objects.filter(project=context.project).count() == 1
    assert agent_get(context.token, "/api/v1/connections").status_code == 200


def test_revoke_is_post_only_and_immediately_blocks_bearer_access(management):
    context = management
    owner = human(context.owner)
    path = f"/projects/{context.project.id}/keys/{context.credential.id}/revoke/"
    assert owner.get(path).status_code == 405
    assert form_post(owner, path).status_code in {200, 302}
    context.credential.refresh_from_db()
    assert context.credential.revoked
    assert agent_get(context.token, "/api/v1/connections").status_code == 401


def test_edit_limits_preserves_spent_reserved_and_ledger_identity(management):
    context = management
    Grant.objects.filter(id=context.grant.id).update(spent_cpu=120, reserved_cpu=60)
    owner = human(context.owner)
    response = form_post(owner, f"/projects/{context.project.id}/", project_data(context, cpu_hours="3", max_cpus="1"))
    assert response.status_code in {200, 302}
    context.grant.refresh_from_db()
    assert context.grant.policy["allowance"]["cpu_core_seconds"] == 10800
    assert context.grant.policy["per_job_limits"]["cpu_cores"] == 1
    assert "maximum_charge_duration_seconds" not in context.grant.policy["per_job_limits"]
    assert (context.grant.spent_cpu, context.grant.reserved_cpu) == (120, 60)
    assert Grant.objects.filter(project=context.project).count() == 1


def test_project_walltime_is_the_only_duration_field_and_legacy_charge_post_is_ignored(management):
    context = management
    owner = human(context.owner)
    page = owner.get(f"/projects/{context.project.id}/")
    assert b'name="charge_minutes"' not in page.content
    response = form_post(owner, f"/projects/{context.project.id}/",
        project_data(context, wall_minutes="10", charge_minutes="1"))
    assert response.status_code == 302
    context.grant.refresh_from_db()
    assert context.grant.policy["per_job_limits"]["time_limit_seconds"] == 600
    assert "maximum_charge_duration_seconds" not in context.grant.policy["per_job_limits"]


@pytest.mark.parametrize("changes", [
    {"cpu_hours": "0"}, {"cpu_hours": "NaN"}, {"cpu_hours": "Infinity"},
    {"cpu_hours": "10000000000000000"}, {"wall_minutes": "0"},
])
def test_invalid_or_insufficient_limit_edits_leave_existing_policy(management, changes):
    context = management
    Grant.objects.filter(id=context.grant.id).update(spent_cpu=120, reserved_cpu=60)
    before = Grant.objects.get(id=context.grant.id).policy
    response = form_post(human(context.owner), f"/projects/{context.project.id}/", project_data(context, **changes))
    assert response.status_code in {200, 400, 409}
    context.grant.refresh_from_db()
    assert context.grant.policy == before
    assert (context.grant.spent_cpu, context.grant.reserved_cpu) == (120, 60)


def test_project_cannot_add_another_owners_cluster(management):
    context = management
    response = form_post(human(context.owner), f"/projects/{context.project.id}/",
                         project_data(context, connections=[str(context.foreign.id)]))
    assert response.status_code in {200, 400}
    assert set(context.project.connections.values_list("id", flat=True)) == {c.id for c in context.connections}


@pytest.mark.parametrize("allowed,expected", [(None, 2), ([], 0), ([0], 1)])
def test_key_cluster_inventory_inherits_or_restricts_project(management, allowed, expected):
    context = management
    context.credential.allowed_connection_ids = None if allowed is None else [str(context.connections[index].id) for index in allowed]
    context.credential.save(update_fields=["allowed_connection_ids"])
    response = agent_get(context.token, "/api/v1/connections")
    assert response.status_code == 200
    assert len(response.json()["connections"]) == expected
    assert agent_get(context.token, f"/api/v1/projects/{context.project.id}/allowance").status_code == 200


def test_key_access_form_cannot_grant_connection_outside_its_project(management):
    context = management
    response = form_post(human(context.owner), f"/projects/{context.project.id}/keys/{context.credential.id}/edit/",
                         key_data(context, cluster_access="restrict", allowed_connections=[str(context.foreign.id)]))
    assert response.status_code in {200, 400}
    context.credential.refresh_from_db()
    assert context.credential.allowed_connection_ids is None


def test_key_access_form_can_deny_all_clusters_without_becoming_inherit(management):
    context = management
    response = form_post(human(context.owner), f"/projects/{context.project.id}/keys/{context.credential.id}/edit/",
                         key_data(context, cluster_access="restrict", allowed_connections=[]))
    assert response.status_code in {200, 302}
    context.credential.refresh_from_db()
    assert context.credential.allowed_connection_ids == []
    assert agent_get(context.token, "/api/v1/connections").json()["connections"] == []


def test_project_can_deny_all_clusters_and_key_cannot_reenable_them(management):
    context = management
    context.credential.allowed_connection_ids = [str(context.connections[0].id)]
    context.credential.save(update_fields=["allowed_connection_ids"])
    response = form_post(human(context.owner), f"/projects/{context.project.id}/", project_data(context, connections=[], environments=[]))
    assert response.status_code in {200, 302}
    context.grant.refresh_from_db()
    assert context.grant.policy["allowed_connection_ids"] == []
    assert not context.project.connections.exists()
    assert agent_get(context.token, "/api/v1/connections").json()["connections"] == []


def test_restricting_key_blocks_existing_job_logs_and_artifacts(management):
    context = management
    context.grant.policy["mode"] = "budgeted"
    context.grant.save(update_fields=["policy"])
    job = prepared(context, context.connections[1], ["result.json"])
    request = execution(context, job)
    attempt = request.attempt
    blob = store_blob(context.project, b'{"synthetic":true}')
    artifact = Artifact.objects.create(attempt=attempt, path="result.json", blob=blob)
    context.credential.allowed_connection_ids = [str(context.connections[0].id)]
    context.credential.save(update_fields=["allowed_connection_ids"])
    paths = [f"/api/v1/jobs/{job.id}", f"/api/v1/jobs/{job.id}/package",
             f"/api/v1/attempts/{attempt.id}/logs", f"/api/v1/attempts/{attempt.id}/artifacts",
             f"/api/v1/artifacts/{artifact.id}/content"]
    for path in paths:
        assert agent_get(context.token, path).status_code in {403, 404}


def test_key_denial_after_prepare_blocks_new_execution_and_dispatch(management):
    context = management
    context.grant.policy["mode"] = "budgeted"
    context.grant.save(update_fields=["policy"])
    job = prepared(context)
    request = execution(context, job)
    lease = services.lease_work(context.worker)["lease"]
    context.credential.allowed_connection_ids = []
    context.credential.save(update_fields=["allowed_connection_ids"])
    response = Client().post(f"/api/v1/jobs/{job.id}/execution-requests", data="{}", content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer " + context.token, HTTP_IDEMPOTENCY_KEY=str(uuid4()))
    assert response.status_code in {403, 404}
    with pytest.raises(BrokerError) as error:
        services.dispatch(context.worker, request.attempt.id, {
            "lease_id": lease["id"], "generation": lease["generation"],
            "manifest_sha256": job.manifest_sha256,
            "connection_revision_sha256": context.connections[0].revision_sha256})
    assert getattr(error.value, "status", None) in {403, 409}
    request.attempt.refresh_from_db()
    assert request.attempt.dispatch_started is False


def test_effective_job_state_tracks_review_and_latest_attempt(management):
    context = management
    job = prepared(context)
    assert services.serialize_job(job)["state"] == "prepared"
    request = execution(context, job)
    assert services.serialize_job(job)["state"] == "awaiting_approval"
    with transaction.atomic():
        services.decide(job, context.owner, {"execution_request_id": str(request.id),
                                           "manifest_sha256": job.manifest_sha256, "approved": False, "comment": "revise"})
    assert services.serialize_job(job)["state"] == "rejected"
    assert b"Rejected" in human(context.owner).get("/").content
    new_request = execution(context, job)
    with transaction.atomic():
        services.decide(job, context.owner, {"execution_request_id": str(new_request.id),
                                           "manifest_sha256": job.manifest_sha256, "approved": True})
    for state in ("authorized", "queued", "running", "succeeded"):
        Attempt.objects.filter(execution_request=new_request).update(state=state)
        job.refresh_from_db()
        result = services.serialize_job(job)
        assert result["state"] == state
        assert result["package_state"] == "prepared"
    assert b"Completed" in human(context.owner).get("/").content
    execution(context, job)
    assert services.serialize_job(job)["state"] == "awaiting_approval"
















def outstanding_discovery_attempt(context, connection):
    job = Job.objects.create(project=context.project, credential=context.credential,
                             request={"name": "unsettled-discovery-safety-job"})
    request = ExecutionRequest.objects.create(job=job, credential=context.credential, state="approved")
    return Attempt.objects.create(job=job, execution_request=request, connection=connection, grant=context.grant,
                                  state="succeeded", accounting_state="settlement_pending", reservation_cpu=95, reservation_gpu=0)
