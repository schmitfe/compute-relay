"""Human management boundaries, one-time keys, and shared project authority.

All identities, credentials, objects, and fake cluster profiles are synthetic.
No test opens a host SSH key, invokes a cluster, or talks to the portal process.
"""

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

from compute_broker.control import services
from compute_broker.control.auth import new_token, token_hash
from compute_broker.control.errors import BrokerError
from compute_broker.control.models import (
    Artifact, Attempt, Connection, Environment, ExecutionRequest, Grant,
    Job, Project, ProjectCredential, User, WorkerIdentity,
)
from compute_broker.control.storage import store_blob


SCOPES = ["objects:write", "jobs:read", "jobs:write", "jobs:execute", "jobs:cancel", "artifacts:read"]


@pytest.fixture
def management(db, settings, tmp_path):
    settings.ARTIFACT_ROOT = tmp_path / "objects"
    owner = User.objects.create_user(username="management-owner", password=None)
    outsider = User.objects.create_user(username="management-outsider", password=None)
    worker = WorkerIdentity.objects.create(name="management-worker", owner=owner,
                                           token_hash=token_hash("synthetic-management-worker"),
                                           profiles=[{"alias": "synthetic-profile", "backend": "fake",
                                                      "allowed_hosts": ["fixture.invalid"], "allowed_users": ["fixtureuser"],
                                                      "allowed_ports": [22], "remote_root_prefixes": ["/fixture/jobs"]}])
    profile = {"partitions": ["debug"], "accounts": ["broker"], "qos": [],
               "maximum_cpus": 2, "maximum_gpus": 1, "maximum_memory_mib": 1024,
               "maximum_time_limit_seconds": 600, "termination_margin_seconds": 5,
               "overtime_margin_seconds": 0, "cleanup_margin_seconds": 30}
    worker.profiles[0].update({"revision_sha256": "a" * 64, "site_profile": profile})
    worker.save(update_fields=["profiles"])
    connections = []
    for index in range(2):
        config = {"backend": "fake", "profile_revision": 1, "site_profile": profile,
                  "state_directory": str(tmp_path / f"fake-{index}")}
        connections.append(Connection.objects.create(owner=owner, worker=worker, name=f"owned-{index}",
                                                     backend="fake", configuration=config,
                                                     revision_sha256=services.digest(config)))
    foreign = Connection.objects.create(owner=outsider, worker=worker, name="private-outsider-cluster",
                                         backend="fake", configuration={}, revision_sha256="f" * 64)
    environment = Environment.objects.create(owner=owner, connection=connections[0], name="management Python", configuration={"interpreter": "/usr/bin/python3", "modules": []},
                                              revision_sha256="e" * 64)
    secondary_environment = Environment.objects.create(owner=owner, connection=connections[1], name="second cluster Python",
        configuration={"interpreter": "/usr/bin/python3", "modules": []}, revision_sha256="e" * 64)
    project = Project.objects.create(name="managed project", owner=owner)
    project.connections.set(connections)
    project.environments.add(environment, secondary_environment)
    policy = {"schema_version": "1.0", "project_id": str(project.id), "revision": 1,
              "mode": "approval_required", "allowed_connection_ids": [str(c.id) for c in connections],
              "allowed_environment_revision_sha256": [environment.revision_sha256],
              "allowed_gpu_types": ["a100"], "per_job_limits": {
                  "cpu_cores": 2, "gpu_devices": 1, "memory_mib": 512,
                  "time_limit_seconds": 600, "maximum_charge_duration_seconds": 660},
              "max_active_jobs": 2, "allowance": {"cpu_core_seconds": 7200, "gpu_device_seconds": 3600},
              "admission_expires_at": (timezone.now() + timedelta(days=3)).isoformat()}
    grant = Grant.objects.create(project=project, policy=policy)
    token = new_token()
    credential = ProjectCredential.objects.create(project=project, name="original agent", prefix=token[:12],
                                                   token_hash=token_hash(token), scopes=SCOPES,
                                                   expires_at=timezone.now() + timedelta(days=1))
    return SimpleNamespace(owner=owner, outsider=outsider, worker=worker, connections=connections,
                           foreign=foreign, environment=environment, secondary_environment=secondary_environment, project=project, grant=grant,
                           credential=credential, token=token)


def human(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    client.get("/login/")
    assert "csrftoken" in client.cookies
    return client


def form_post(client, path, data=None):
    return client.post(path, data or {}, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)


def project_data(context, **changes):
    data = {"name": "updated managed project", "mode": "approval_required", "cpu_hours": "2", "gpu_hours": "1",
            "max_cpus": "2", "max_gpus": "1", "memory_mib": "512", "wall_minutes": "10", "charge_minutes": "11",
            "max_active_jobs": "2", "max_total_jobs": "1000", "expires_at": (timezone.now() + timedelta(days=3)).isoformat(),
            "connections": [str(c.id) for c in context.connections], "environments": [str(context.environment.id)],
            "allowed_gpu_types": "a100"}
    data.update(changes)
    return data


def key_data(context, **changes):
    data = {"name": "separate agent", "expires_at": (timezone.now() + timedelta(days=2)).isoformat(),
            "scopes": SCOPES, "cluster_access": "inherit", "allowed_connections": [], "execution_mode": "inherit", "submission_id": str(uuid4())}
    data.update(changes)
    return data


def issued(response):
    assert response.status_code in {200, 201}
    secret = response.context["secret"]
    credential = response.context["credential"]
    assert secret.startswith("cbp_") and len(secret) >= 40
    assert "no-store" in response.headers.get("Cache-Control", "").lower()
    assert credential.token_hash == token_hash(secret)
    assert credential.prefix != secret[:len(credential.prefix)]
    return secret, credential


def assert_not_persisted(secret):
    for model in apps.get_models():
        for record in model._default_manager.values():
            assert secret not in json.dumps(record, default=str), model._meta.label
    for session in Session.objects.all():
        assert secret not in json.dumps(session.get_decoded(), default=str)


def agent_get(token, path):
    return Client().get(path, HTTP_AUTHORIZATION="Bearer " + token)


def prepared(context, connection=None, outputs=None, *, name="management-test"):
    source = b"raise RuntimeError('management tests must never execute uploaded source')\n"
    blob = store_blob(context.project, source)
    connection = connection or context.connections[0]
    environment = context.secondary_environment if connection.id == context.connections[1].id else context.environment
    document = {"schema_version": "1.0", "project_id": str(context.project.id), "connection_id": str(connection.id),
                "name": name, "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
                "environment": {"id": str(environment.id), "revision_sha256": environment.revision_sha256},
                "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
                "slurm": {"partition": "debug", "account": "broker"},
                "files": [{"path": "main.py", "sha256": blob.sha256, "size_bytes": blob.size_bytes, "executable": False}],
                "outputs": outputs or []}
    created, _ = services.create_job(context.credential, document)
    job = Job.objects.get(id=created["id"])
    services.prepare_job(job)
    job.refresh_from_db()
    return job


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


def test_cluster_form_accepts_only_an_advertised_scoped_profile(management):
    from compute_broker.control.forms import ClusterForm
    context = management
    form = ClusterForm({"name": "new fixture", "worker": str(context.worker.id),
                        "credential_profile": "synthetic-profile", "host": "fixture.invalid",
                        "user": "fixtureuser", "port": 22, "remote_root": "/fixture/jobs/project"}, user=context.owner)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["profile"]["alias"] == "synthetic-profile"
    assert form.cleaned_data["profile"]["backend"] == "fake"


@pytest.mark.parametrize("changes", [
    {"credential_profile": "unknown-profile"}, {"host": "unapproved.invalid"},
    {"host": "fixture.invalid;whoami"}, {"user": "another-user"}, {"port": 2222},
    {"remote_root": "/fixture/jobs-other"}, {"remote_root": "/fixture/jobs/../escape"},
    {"remote_root": "/fixture//jobs"},
])
def test_cluster_form_rejects_targets_outside_worker_profile(management, changes):
    from compute_broker.control.forms import ClusterForm
    context = management
    data = {"name": "new fixture", "worker": str(context.worker.id), "credential_profile": "synthetic-profile",
            "host": "fixture.invalid", "user": "fixtureuser", "port": 22, "remote_root": "/fixture/jobs/project"}
    data.update(changes)
    assert not ClusterForm(data, user=context.owner).is_valid()


def test_cluster_form_cannot_select_another_users_private_worker(management):
    from compute_broker.control.forms import ClusterForm
    context = management
    worker = WorkerIdentity.objects.create(name="outsider private worker", owner=context.outsider,
                                           token_hash=token_hash("outsider-private-worker"), profiles=context.worker.profiles)
    data = {"name": "new fixture", "worker": str(worker.id), "credential_profile": "synthetic-profile",
            "host": "fixture.invalid", "user": "fixtureuser", "port": 22, "remote_root": "/fixture/jobs/project"}
    assert not ClusterForm(data, user=context.owner).is_valid()


@pytest.mark.parametrize("changes", [
    {"interpreter": "python3 -c bad"}, {"interpreter": "/usr/bin/../bad"},
    {"modules": "python/3.12; touch /tmp/bad"}, {"modules": "python/$(whoami)"},
    {"module_init": "/home/user/untrusted.sh"},
])
def test_environment_form_rejects_command_fragments_and_untrusted_hooks(management, changes):
    from compute_broker.control.forms import EnvironmentForm
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
        assert agent_get(secret, f"/api/v1/projects/{context.project.id}/allowance").json() == original
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


@pytest.mark.parametrize("mode", ["inherit", "approval_required", "automatic"])
def test_key_creation_and_access_editor_store_explicit_execution_confirmation_mode(management, mode):
    context = management
    owner = human(context.owner)
    _, key = issued(form_post(owner, f"/projects/{context.project.id}/keys/new/", key_data(context, execution_mode=mode)))
    assert key.execution_mode == mode
    edit = owner.get(f"/projects/{context.project.id}/keys/{key.id}/edit/")
    assert edit.status_code == 200 and edit.context["form"].initial["execution_mode"] == mode
    other_mode = "approval_required" if mode != "approval_required" else "automatic"
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


def test_automatic_key_cannot_bypass_project_required_review(management):
    context = management
    context.credential.execution_mode = "automatic"
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
    assert (context.grant.spent_cpu, context.grant.reserved_cpu) == (120, 60)
    assert Grant.objects.filter(project=context.project).count() == 1


@pytest.mark.parametrize("changes", [
    {"cpu_hours": "0"}, {"cpu_hours": "NaN"}, {"cpu_hours": "Infinity"},
    {"cpu_hours": "10000000000000000"}, {"wall_minutes": "0"},
    {"charge_minutes": "1", "wall_minutes": "10"},
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


def queued_discovery(context):
    config = {"backend": "fake", "credential_profile": "synthetic-profile",
              "ssh": {"host": "fixture.invalid", "user": "fixtureuser", "port": 22},
              "remote_root": "/fixture/jobs/project"}
    return Connection.objects.create(owner=context.owner, worker=context.worker, name="discovery fixture", backend="fake",
                                     configuration=config, revision_sha256=services.digest(config), discovery_state="queued")


def discovery_report(connection, worker, lease, **changes):
    config = {**connection.configuration, "profile_revision": 1,
              "site_profile": worker.profiles[0]["site_profile"],
              "credential_profile_revision_sha256": worker.profiles[0]["revision_sha256"]}
    report = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "discovered",
              "configuration": config, "connection_revision_sha256": services.digest(config),
              "discovery_snapshot": {"scope": "synthetic", "partitions": [{"name": "debug"}],
                                     "software": {"scope": "login_node", "verified_on_compute": False}}}
    report.update(changes)
    return report


def test_owner_enrolls_cluster_without_private_credential_material(management):
    context = management
    before = Connection.objects.count()
    data = {"name": "new enrolled fixture", "worker": str(context.worker.id), "credential_profile": "synthetic-profile",
            "host": "fixture.invalid", "user": "fixtureuser", "port": 22, "remote_root": "/fixture/jobs/project"}
    with patch("subprocess.run", side_effect=AssertionError("cluster enrollment must not execute SSH locally")):
        response = form_post(human(context.owner), "/clusters/new/", data)
    assert response.status_code == 302
    assert Connection.objects.count() == before + 1
    connection = Connection.objects.get(name=data["name"])
    assert connection.owner_id == context.owner.id
    assert connection.worker_id == context.worker.id
    assert connection.configuration["credential_profile"] == "synthetic-profile"
    assert connection.backend == "fake"
    assert connection.discovery_state == "queued"
    assert "identity_file" not in json.dumps(connection.configuration)
    assert "known_hosts" not in json.dumps(connection.configuration)


def test_discovery_lease_is_worker_scoped_and_replay_is_rejected(management):
    from compute_broker.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    stranger = WorkerIdentity.objects.create(name="unrelated worker", owner=context.outsider,
                                             token_hash=token_hash("unrelated-discovery-worker"))
    assert lease_discovery(stranger) is None
    lease = lease_discovery(context.worker)["lease"]
    assert lease["operation"] == "discover"
    assert lease["connection_id"] == str(connection.id)
    report = discovery_report(connection, context.worker, lease)
    with pytest.raises(BrokerError) as denied:
        report_discovery(stranger, connection.id, report)
    assert denied.value.status == 404
    with pytest.raises(BrokerError) as stale:
        report_discovery(context.worker, connection.id, {**report, "generation": lease["generation"] - 1})
    assert stale.value.status == 409
    assert report_discovery(context.worker, connection.id, report)["accepted"] is True
    connection.refresh_from_db()
    assert connection.discovery_state == "ready"
    assert connection.discovery_snapshot["software"]["verified_on_compute"] is False
    with pytest.raises(BrokerError) as duplicate:
        report_discovery(context.worker, connection.id, report)
    assert duplicate.value.status == 409


def test_rediscovery_fences_old_lease_and_failure_preserves_last_good_snapshot(management):
    from compute_broker.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    connection.discovery_snapshot = {"last_good": "preserve this synthetic metadata"}
    connection.save(update_fields=["discovery_snapshot"])
    old = lease_discovery(context.worker)["lease"]
    response = form_post(human(context.owner), f"/clusters/{connection.id}/discover/")
    assert response.status_code == 302
    with pytest.raises(BrokerError) as stale:
        report_discovery(context.worker, connection.id, discovery_report(connection, context.worker, old))
    assert stale.value.status == 409
    new = lease_discovery(context.worker)["lease"]
    report_discovery(context.worker, connection.id, {"lease_id": new["id"], "generation": new["generation"],
                                                    "outcome": "discovery_failed", "error_code": "AUTH_REQUIRED"})
    connection.refresh_from_db()
    assert connection.discovery_state == "failed"
    assert connection.discovery_snapshot == {"last_good": "preserve this synthetic metadata"}
    assert connection.discovery_error == "AUTH_REQUIRED"


def test_discovery_report_rejects_secret_fields_and_changed_targets(management):
    from compute_broker.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    lease = lease_discovery(context.worker)["lease"]
    report = discovery_report(connection, context.worker, lease)
    bad_config = {**report["configuration"], "identity_file": "/private/worker/key"}
    with pytest.raises(BrokerError):
        report_discovery(context.worker, connection.id, {**report, "configuration": bad_config})
    changed = {**report["configuration"], "ssh": {"host": "different.invalid", "user": "fixtureuser", "port": 22}}
    with pytest.raises(BrokerError) as target:
        report_discovery(context.worker, connection.id, {**report, "configuration": changed})
    assert target.value.status == 409
    connection.refresh_from_db()
    assert connection.discovery_snapshot == {}


def test_expired_discovery_report_cannot_replace_last_good_snapshot(management):
    from compute_broker.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    connection.discovery_snapshot = {"last_good": True}
    connection.save(update_fields=["discovery_snapshot"])
    lease = lease_discovery(context.worker)["lease"]
    Connection.objects.filter(id=connection.id).update(discovery_lease_expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(BrokerError) as expired:
        report_discovery(context.worker, connection.id, discovery_report(connection, context.worker, lease))
    assert expired.value.status == 409
    connection.refresh_from_db()
    assert connection.discovery_snapshot == {"last_good": True}


def outstanding_discovery_attempt(context, connection):
    job = Job.objects.create(project=context.project, credential=context.credential,
                             request={"name": "unsettled-discovery-safety-job"})
    request = ExecutionRequest.objects.create(job=job, credential=context.credential, state="approved")
    return Attempt.objects.create(job=job, execution_request=request, connection=connection, grant=context.grant,
                                  state="succeeded", accounting_state="settlement_pending", reservation_cpu=95, reservation_gpu=0)


def test_discovery_cannot_change_execution_profile_until_existing_accounting_settles(management):
    from compute_broker.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    initial = lease_discovery(context.worker)["lease"]
    report_discovery(context.worker, connection.id, discovery_report(connection, context.worker, initial))
    connection.refresh_from_db()
    original_revision, original_snapshot = connection.revision_sha256, connection.discovery_snapshot
    attempt = outstanding_discovery_attempt(context, connection)
    form_post(human(context.owner), f"/clusters/{connection.id}/discover/")
    lease = lease_discovery(context.worker)["lease"]
    context.worker.profiles[0]["revision_sha256"] = "b" * 64
    context.worker.profiles[0]["site_profile"] = {**context.worker.profiles[0]["site_profile"], "maximum_cpus": 1}
    context.worker.save(update_fields=["profiles"])
    report = discovery_report(connection, context.worker, lease, discovery_snapshot={"updated": True})
    with pytest.raises(BrokerError) as busy:
        report_discovery(context.worker, connection.id, report)
    assert busy.value.code == "CONNECTION_BUSY" and busy.value.status == 409
    connection.refresh_from_db()
    assert connection.revision_sha256 == original_revision
    assert connection.discovery_snapshot == original_snapshot
    attempt.accounting_state = "settled"
    attempt.save(update_fields=["accounting_state"])
    assert report_discovery(context.worker, connection.id, report)["accepted"]
    connection.refresh_from_db()
    assert connection.revision_sha256 != original_revision
    assert connection.discovery_snapshot == {"updated": True}


def test_discovery_can_refresh_snapshot_without_changing_unsettled_execution_profile(management):
    from compute_broker.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    initial = lease_discovery(context.worker)["lease"]
    report_discovery(context.worker, connection.id, discovery_report(connection, context.worker, initial))
    connection.refresh_from_db()
    original_revision = connection.revision_sha256
    outstanding_discovery_attempt(context, connection)
    form_post(human(context.owner), f"/clusters/{connection.id}/discover/")
    lease = lease_discovery(context.worker)["lease"]
    report = discovery_report(connection, context.worker, lease, discovery_snapshot={"new_inventory_only": True})
    assert report_discovery(context.worker, connection.id, report)["accepted"]
    connection.refresh_from_db()
    assert connection.revision_sha256 == original_revision
    assert connection.discovery_snapshot == {"new_inventory_only": True}


def test_profile_advertisement_requires_worker_and_rejects_secret_fields(management):
    context = management
    endpoint = "/internal/v1/worker/profiles"
    payload = {"profiles": [{**context.worker.profiles[0], "private_key": "SYNTHETIC_PRIVATE_KEY_SENTINEL"}]}
    agent = Client().post(endpoint, data=json.dumps(payload), content_type="application/json",
                          HTTP_AUTHORIZATION="Bearer " + context.token)
    assert agent.status_code == 401
    response = Client().post(endpoint, data=json.dumps(payload), content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer synthetic-management-worker")
    assert response.status_code == 400
    context.worker.refresh_from_db()
    assert "SYNTHETIC_PRIVATE_KEY_SENTINEL" not in json.dumps(context.worker.profiles)
    assert b"SYNTHETIC_PRIVATE_KEY_SENTINEL" not in response.content


def test_saved_discovery_is_escaped_when_rendered_in_cluster_portal(management):
    context = management
    connection = context.connections[0]
    payload = "<script>syntheticSnapshotAttack()</script>"
    connection.discovery_snapshot = {"notice": payload, "software": {"scope": "login_node", "verified_on_compute": False}}
    connection.save(update_fields=["discovery_snapshot"])
    response = human(context.owner).get(f"/clusters/{connection.id}/")
    assert response.status_code == 200
    assert payload.encode() not in response.content
    assert b"&lt;script&gt;" in response.content


@pytest.mark.parametrize("kind,details", [
    ("system", {"interpreter": "/usr/bin/python3"}),
    ("modules", {"interpreter": "python3", "modules": "python/3.12", "module_init": "/etc/profile.d/modules.sh"}),
    ("venv", {"interpreter": "/opt/venvs/test/bin/python", "prefix": "/opt/venvs/test"}),
    ("conda", {"interpreter": "/opt/conda/envs/test/bin/python", "prefix": "/opt/conda/envs/test", "conda_executable": "/opt/conda/bin/conda"}),
    ("poetry", {"interpreter": "/opt/poetry/test/bin/python", "prefix": "/opt/poetry/test"}),
])
def test_environment_recipe_is_stored_immutably_without_local_execution(management, kind, details):
    context = management
    data = {"name": f"existing {kind}", "connection": str(context.connections[0].id), "kind": kind, **details}
    with patch("subprocess.run", side_effect=AssertionError("environment enrollment must not execute software locally")):
        response = form_post(human(context.owner), f"/clusters/{context.connections[0].id}/environments/new/", data)
    assert response.status_code == 302
    environment = Environment.objects.get(name=data["name"])
    assert environment.owner_id == context.owner.id
    assert environment.connection_id == context.connections[0].id
    assert environment.revision_sha256 == services.digest(environment.configuration)
    assert environment.configuration["kind"] == kind


def test_modules_recipe_saves_exact_search_paths_in_immutable_digest(management):
    context = management
    data = {"name": "module search path recipe", "connection": str(context.connections[0].id),
            "kind": "modules", "interpreter": "python3", "modules": "Python/3.12\nSciPy/1.14",
            "module_init": "/etc/profile.d/z00_lmod.sh", "module_paths": "/opt/site/modules\n/opt/shared/modulefiles"}
    with patch("subprocess.run", side_effect=AssertionError("saving paths must not execute module hooks")):
        response = form_post(human(context.owner), f"/clusters/{context.connections[0].id}/environments/new/", data)
    assert response.status_code == 302
    environment = Environment.objects.get(name=data["name"])
    assert environment.configuration["module_paths"] == ["/opt/site/modules", "/opt/shared/modulefiles"]
    assert environment.configuration["modules"] == ["Python/3.12", "SciPy/1.14"]
    assert environment.revision_sha256 == services.digest(environment.configuration)
    changed = {**environment.configuration, "module_paths": ["/opt/other/modulefiles"]}
    assert services.digest(changed) != environment.revision_sha256


def test_modules_environment_form_prefills_only_saved_owner_snapshot(management):
    context = management
    connection = context.connections[0]
    connection.discovery_snapshot = {"software": {"modules": {
        "init_path": "/etc/profile.d/z00_lmod.sh", "module_paths": ["/opt/site/modules", "/opt/shared/modulefiles"]}}}
    connection.save(update_fields=["discovery_snapshot"])
    page = human(context.owner).get(f"/clusters/{connection.id}/environments/new/", {"kind": "modules"})
    assert page.status_code == 200
    form = page.context["form"]
    assert form.initial["interpreter"] == "python3"
    assert form.initial["module_init"] == "/etc/profile.d/z00_lmod.sh"
    assert form.initial["module_paths"] == "/opt/site/modules\n/opt/shared/modulefiles"
    connection.owner = context.outsider
    connection.save(update_fields=["owner"])
    private = human(context.owner).get(f"/clusters/{connection.id}/environments/new/", {"kind": "modules"})
    assert private.status_code == 404
    assert b"/opt/site/modules" not in private.content


@pytest.mark.parametrize("connection_id", ["not-a-uuid", "", str(uuid4())])
def test_environment_form_handles_missing_or_invalid_connection_without_server_error(management, connection_id):
    page = human(management.owner).get("/environments/new/", {"connection": connection_id, "kind": "modules"})
    assert page.status_code in {302, 400, 404}


@pytest.mark.parametrize("kind,details", [
    ("system", {"interpreter": "/usr/bin/python3"}),
    ("venv", {"interpreter": "/opt/venvs/test/bin/python", "prefix": "/opt/venvs/test"}),
])
def test_nonmodule_environment_rejects_module_search_paths(management, kind, details):
    from compute_broker.control.forms import EnvironmentForm
    context = management
    form = EnvironmentForm({"name": "inconsistent environment", "connection": str(context.connections[0].id),
                            "kind": kind, **details, "module_paths": "/opt/site/modules"}, user=context.owner)
    assert not form.is_valid()
    assert "module_paths" in form.errors


@pytest.mark.parametrize("path", ["/opt/site/../escape", "/opt//modules", "/opt/modules/", "/opt/modules:/other", "$(touch injected)"])
def test_module_search_path_enrollment_rejects_noncanonical_or_shell_paths(management, path):
    context = management
    before = Environment.objects.count()
    response = form_post(human(context.owner), f"/clusters/{context.connections[0].id}/environments/new/", {
        "name": "bad module search path", "connection": str(context.connections[0].id), "kind": "modules",
        "interpreter": "python3", "modules": "Python/3.12", "module_init": "/etc/profile.d/modules.sh",
        "module_paths": path})
    assert response.status_code == 200
    assert response.context["form"].errors
    assert Environment.objects.count() == before
