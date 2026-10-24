"""Human diagnostics use normal packages, approval, ledger and worker reports."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import transaction
from django.test import Client
from django.urls import path
from django.utils import timezone

from compute_relay.control import cluster_checks, services
from compute_relay.control.models import (Artifact, Attempt, ClusterCheckConfiguration, ExecutionRequest,
    Grant, Job, LedgerEntry, Project, ProjectCredential, User)
from compute_relay.control.errors import BrokerError
from compute_relay.control.storage import read_blob, store_blob
from compute_relay.control.urls import urlpatterns as core_urls
from test_management import management, human, form_post


urlpatterns = [*core_urls, path("clusters/<uuid:connection_id>/test/", cluster_checks.check)]


@pytest.fixture
def diagnostic(management, settings):
    settings.ROOT_URLCONF = __name__
    context = management
    context.connection = context.connections[0]
    context.connection.configuration = {**context.connection.configuration, "site_profile": {
        **context.connection.configuration["site_profile"], "automatic_execution_allowed": True}}
    context.connection.save(update_fields=["configuration"])
    context.path = f"/clusters/{context.connection.id}/test/"
    return context


def data(context, **changes):
    return {"submission_id": str(uuid4()), "connection_revision": context.connection.revision_sha256,
        "partition": "debug", "account": "broker", "qos": "", "environment": "", **changes}


def prepared(context, **changes):
    response = form_post(human(context.owner), context.path, data(context, **changes))
    assert response.status_code == 302, response.content.decode()
    configuration = ClusterCheckConfiguration.objects.get(connection=context.connection)
    job = Job.objects.filter(project=configuration.project).latest("created_at")
    assert response.url == f"/review/{job.id}/"
    return configuration, job


def test_get_is_preview_only_and_does_not_connect_or_create_allowance(diagnostic):
    context = diagnostic
    counts = (Project.objects.count(), Job.objects.count(), ProjectCredential.objects.count())
    with patch("subprocess.run", side_effect=AssertionError("no scheduler calls")), patch("subprocess.Popen", side_effect=AssertionError("no SSH")):
        response = human(context.owner).get(context.path)
    assert response.status_code == 200
    assert b"Prepare test for review" in response.content
    assert b"one-minute scheduler walltime" in response.content
    assert b"time.sleep(2)" in response.content
    assert (Project.objects.count(), Job.objects.count(), ProjectCredential.objects.count()) == counts
    assert not ClusterCheckConfiguration.objects.exists()


def test_prepare_uses_finite_dedicated_approval_project_and_exact_normal_package(diagnostic):
    context = diagnostic
    with patch("subprocess.run", side_effect=AssertionError("no scheduler calls")), patch("subprocess.Popen", side_effect=AssertionError("no SSH")):
        config, job = prepared(context)
    policy = config.project.grant.policy
    assert policy["allowance"] == {"cpu_core_seconds": 3600, "gpu_device_seconds": 0}
    assert policy["max_total_jobs"] == 20 and policy["max_active_jobs"] == 1 and policy["mode"] == "approval_required"
    assert config.credential.issued_by_id == context.owner.id
    assert config.credential.execution_mode == "approval_required"
    assert len(config.credential.token_hash) == 64
    assert job.request["resources"] == {"nodes": 1, "cpus": 2, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60}
    assert job.manifest and job.state == "prepared"
    assert job.request["outputs"] == ["cluster-check.json"]
    assert read_blob(job.files.get(path="cluster-check.py").blob) == cluster_checks.SOURCE
    assert job.execution_requests.get().state == "awaiting_approval"
    assert not Attempt.objects.filter(job=job).exists()
    config.project.grant.refresh_from_db()
    assert config.project.grant.reserved_cpu == 120 and config.project.grant.spent_cpu == 0
    assert job.request["environment"]["id"] == str(config.environment_id)


def test_cpu_one_site_uses_single_cpu(diagnostic):
    context = diagnostic
    context.connection.configuration["site_profile"]["maximum_cpus"] = 1
    context.connection.save(update_fields=["configuration"])
    _, job = prepared(context)
    assert job.request["resources"]["cpus"] == 1


def test_owner_can_select_existing_python_recipe_without_cross_cluster_access(diagnostic):
    config, job = prepared(diagnostic, environment=str(diagnostic.environment.id))
    assert config.environment_id == diagnostic.environment.id
    assert job.request["environment"]["id"] == str(diagnostic.environment.id)
    response = form_post(human(diagnostic.owner), diagnostic.path, data(diagnostic, environment=str(diagnostic.secondary_environment.id)))
    assert response.status_code == 200 and "environment" in response.context["form"].errors


def test_system_recipe_uses_saved_candidate_without_executing_it(diagnostic):
    diagnostic.connection.discovery_snapshot = {"software": {"python": [{"kind": "system", "interpreter": "/opt/site/bin/python3"}]}}
    diagnostic.connection.save(update_fields=["discovery_snapshot"])
    config, _ = prepared(diagnostic)
    assert config.environment.configuration["interpreter"] == "/opt/site/bin/python3"


def test_same_submission_replays_one_job_one_request_and_does_not_reset_funds(diagnostic):
    context = diagnostic
    client = human(context.owner)
    posted = data(context)
    first = form_post(client, context.path, posted)
    config = ClusterCheckConfiguration.objects.get(connection=context.connection)
    grant = config.project.grant
    grant.spent_cpu = 17
    grant.save(update_fields=["spent_cpu"])
    key_expiry = config.credential.expires_at
    second = form_post(client, context.path, posted)
    assert first.status_code == second.status_code == 302 and first.url == second.url
    assert Job.objects.filter(project=config.project).count() == 1
    assert ExecutionRequest.objects.filter(job__project=config.project).count() == 1
    assert ClusterCheckConfiguration.objects.count() == 1
    grant.refresh_from_db()
    assert grant.spent_cpu == 17 and grant.policy["allowance"]["cpu_core_seconds"] == 3600
    config.credential.refresh_from_db()
    assert config.credential.expires_at == key_expiry


def test_same_submission_with_changed_environment_conflicts(diagnostic):
    context = diagnostic
    client = human(context.owner)
    posted = data(context)
    assert form_post(client, context.path, posted).status_code == 302
    config = ClusterCheckConfiguration.objects.get(connection=context.connection)
    config.project.environments.add(context.environment)
    grant = config.project.grant
    grant.policy["allowed_environment_revision_sha256"].append(context.environment.revision_sha256)
    grant.save(update_fields=["policy"])
    response = form_post(client, context.path, {**posted, "environment": str(context.environment.id)})
    assert response.status_code == 409
    assert Job.objects.filter(project=config.project).count() == 1


@pytest.mark.parametrize("identity", ["outsider", "superuser"])
def test_personal_check_cannot_use_foreign_connection_even_for_admin(diagnostic, identity):
    user = diagnostic.outsider if identity == "outsider" else User.objects.create_superuser(username="check-admin", password=None)
    client = human(user)
    assert client.get(diagnostic.path).status_code == 404
    assert form_post(client, diagnostic.path, data(diagnostic)).status_code == 404
    assert not ClusterCheckConfiguration.objects.exists()


def test_human_session_and_csrf_required_no_project_key_route(diagnostic):
    context = diagnostic
    client = Client(enforce_csrf_checks=True)
    assert client.post(context.path, data(context), HTTP_AUTHORIZATION="Bearer " + context.token).status_code == 403
    assert human(context.owner).post(context.path, data(context)).status_code == 403
    assert not ClusterCheckConfiguration.objects.exists()


@pytest.mark.parametrize("field", ["enabled", "setup_complete", "configuration_confirmed"])
def test_incomplete_disabled_or_unconfirmed_connection_blocks_check(diagnostic, field):
    setattr(diagnostic.connection, field, False)
    diagnostic.connection.save(update_fields=[field])
    page = human(diagnostic.owner).get(diagnostic.path)
    assert page.status_code == 200 and page.context["blocked_reason"]
    assert form_post(human(diagnostic.owner), diagnostic.path, data(diagnostic)).status_code == 409
    assert not ClusterCheckConfiguration.objects.exists()


@pytest.mark.parametrize("change", [{"allocated_gpus": 1},
    {"allocated_cpus": 128, "maximum_cpus": 128}, {"maximum_memory_mib": 64}])
def test_unsafe_or_unaffordable_cpu_only_shape_is_not_given_a_bigger_grant(diagnostic, change):
    diagnostic.connection.configuration["site_profile"].update(change)
    diagnostic.connection.save(update_fields=["configuration"])
    assert form_post(human(diagnostic.owner), diagnostic.path, data(diagnostic)).status_code == 409
    assert not ClusterCheckConfiguration.objects.exists()


def test_cluster_check_uses_requested_walltime_and_explicit_allocation(diagnostic):
    profile = diagnostic.connection.configuration["site_profile"]
    profile.update(automatic_execution_allowed=False)
    profile["allocated_cpus"] = 4
    profile["maximum_cpus"] = 4
    diagnostic.connection.save(update_fields=["configuration"])
    plan = cluster_checks.resource_plan(diagnostic.connection)
    assert plan["reservation_cpu_seconds"] == 240
    assert plan["profile"]["automatic_execution_allowed"] is False
    config, job = prepared(diagnostic)
    assert config.project.grant.policy["per_job_limits"] == {"cpu_cores": 4, "gpu_devices": 0,
        "memory_mib": 128, "time_limit_seconds": 60}
    assert job.manifest["reservation"]["cpu_core_seconds"] == 240
    assert job.execution_requests.get().state == "awaiting_approval"


def test_stale_preview_revision_creates_no_diagnostic_project(diagnostic):
    response = form_post(human(diagnostic.owner), diagnostic.path, data(diagnostic, connection_revision="a" * 64))
    assert response.status_code == 409
    assert not ClusterCheckConfiguration.objects.exists()


@pytest.mark.parametrize("mutation", ["expired", "paused", "revoked", "automatic"])
def test_existing_diagnostic_policy_key_and_expiry_are_never_renewed_or_replaced(diagnostic, mutation):
    config, _ = prepared(diagnostic)
    grant = config.project.grant
    if mutation == "expired":
        grant.policy["admission_expires_at"] = (timezone.now() - timedelta(seconds=1)).isoformat()
        grant.save(update_fields=["policy"])
    elif mutation == "paused":
        config.project.paused = True
        config.project.save(update_fields=["paused"])
    elif mutation == "revoked":
        config.credential.revoked = True
        config.credential.save(update_fields=["revoked"])
    else:
        grant.policy["mode"] = "budgeted"
        grant.save(update_fields=["policy"])
    original = Grant.objects.get(project=config.project).policy
    response = form_post(human(diagnostic.owner), diagnostic.path, data(diagnostic))
    assert response.status_code == 409
    assert ClusterCheckConfiguration.objects.count() == 1
    assert Grant.objects.get(project=config.project).policy == original
    assert Job.objects.filter(project=config.project).count() == 1


def test_upload_and_preparation_run_outside_idempotent_transactions(diagnostic, monkeypatch):
    from django.db import connection
    calls = []
    original_source, original_generated = cluster_checks.store_blob, services.store_blob
    def source(*args, **kwargs):
        calls.append(connection.in_atomic_block)
        return original_source(*args, **kwargs)
    def generated(*args, **kwargs):
        calls.append(connection.in_atomic_block)
        return original_generated(*args, **kwargs)
    monkeypatch.setattr(cluster_checks, "store_blob", source)
    monkeypatch.setattr(services, "store_blob", generated)
    prepared(diagnostic)
    # pytest's fixture outer transaction is expected; test the production path
    # separately with transaction=True below when no test wrapper is present.
    assert len(calls) == 3


@pytest.mark.django_db(transaction=True)
def test_diagnostic_reservations_commit_without_outer_idempotency_transaction(diagnostic, monkeypatch):
    from django.db import connection
    calls = []
    original_source, original_generated = cluster_checks.store_blob, services.store_blob
    def checked(original):
        def inner(*args, **kwargs):
            calls.append(connection.in_atomic_block)
            return original(*args, **kwargs)
        return inner
    monkeypatch.setattr(cluster_checks, "store_blob", checked(original_source))
    monkeypatch.setattr(services, "store_blob", checked(original_generated))
    prepared(diagnostic)
    assert calls == [False, False, False]


def test_normal_approval_reserves_and_terminal_report_settles_without_bypass(diagnostic):
    context = diagnostic
    config, job = prepared(context)
    execution = job.execution_requests.get()
    with transaction.atomic():
        services.decide(job, context.owner, {"execution_request_id": str(execution.id),
            "manifest_sha256": job.manifest_sha256, "approved": True})
    attempt = Attempt.objects.get(job=job)
    grant = Grant.objects.get(project=config.project)
    assert grant.reserved_cpu == job.manifest["reservation"]["cpu_core_seconds"]
    assert services.allowance(config.project)["jobs"]["admitted"] == 1
    lease = services.lease_work(context.worker)["lease"]
    services.dispatch(context.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "manifest_sha256": job.manifest_sha256, "connection_revision_sha256": context.connection.revision_sha256})
    native = {"job_id": "17", "attempt_id": str(attempt.id), "cluster": "simulation"}
    services.report(context.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "outcome": "submitted", "native_identity": native})
    Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now() - timedelta(seconds=1))
    lease = services.lease_work(context.worker)["lease"]
    services.report(context.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "outcome": "observed", "observation": {"state": "succeeded", "terminal": True,
            "accounting": {"final": True, "cpu_core_seconds": 4, "gpu_device_seconds": 0}}})
    grant.refresh_from_db()
    assert grant.spent_cpu == 4 and grant.reserved_cpu == grant.spent_gpu == grant.reserved_gpu == 0
    assert LedgerEntry.objects.filter(grant=grant).count() >= 2
    recent = human(context.owner).get(context.path)
    assert f"/review/{job.id}/".encode() in recent.content


def test_rejection_does_not_create_attempt_or_spend_diagnostic_allowance(diagnostic):
    config, job = prepared(diagnostic)
    execution = job.execution_requests.get()
    with transaction.atomic():
        services.decide(job, diagnostic.owner, {"execution_request_id": str(execution.id),
            "manifest_sha256": job.manifest_sha256, "approved": False, "comment": "Choose another partition"})
    assert not Attempt.objects.filter(job=job).exists()
    assert Grant.objects.get(project=config.project).spent_cpu == 0
    assert services.serialize_job(job)["execution_requests"][0]["comment"] == "Choose another partition"


def test_core_lifetime_and_budget_checks_still_reject_diagnostic_approval(diagnostic):
    config, job = prepared(diagnostic)
    grant = config.project.grant
    grant.policy["max_total_jobs"] = 0
    grant.save(update_fields=["policy"])
    with pytest.raises(BrokerError, match="lifetime"):
        with transaction.atomic():
            services.decide(job, diagnostic.owner, {"execution_request_id": str(job.execution_requests.get().id),
                "manifest_sha256": job.manifest_sha256, "approved": True})
    grant.refresh_from_db()
    assert grant.reserved_cpu == 120 and not Attempt.objects.filter(job=job).exists()
    grant.policy["max_total_jobs"] = 20
    grant.policy["allowance"]["cpu_core_seconds"] = 1
    grant.save(update_fields=["policy"])
    with pytest.raises(BrokerError, match="allowance"):
        with transaction.atomic():
            services.decide(job, diagnostic.owner, {"execution_request_id": str(job.execution_requests.get().id),
                "manifest_sha256": job.manifest_sha256, "approved": True})
    assert not Attempt.objects.filter(job=job).exists()


def test_results_are_bounded_escaped_owner_only_and_need_no_revealed_token(diagnostic):
    config, job = prepared(diagnostic)
    with transaction.atomic():
        services.decide(job, diagnostic.owner, {"execution_request_id": str(job.execution_requests.get().id),
            "manifest_sha256": job.manifest_sha256, "approved": True})
    attempt = Attempt.objects.get(job=job)
    attempt.stdout = "<script>not executable</script>"
    attempt.save(update_fields=["stdout"])
    blob = store_blob(config.project, b'{"marker":"compute-relay-cluster-check-v1","note":"<script>"}\n')
    Artifact.objects.create(attempt=attempt, path="cluster-check.json", blob=blob)
    page = human(diagnostic.owner).get(diagnostic.path)
    assert page.status_code == 200
    assert b"compute-relay-cluster-check-v1" in page.content
    assert b"&lt;script&gt;not executable&lt;/script&gt;" in page.content
    assert b"<script>not executable</script>" not in page.content
    assert blob.sha256.encode() in page.content
    assert human(diagnostic.outsider).get(diagnostic.path).status_code == 404


def test_failed_review_and_test_page_show_native_cause_before_prepared_files(diagnostic):
    _, job = prepared(diagnostic)
    with transaction.atomic():
        services.decide(job, diagnostic.owner, {"execution_request_id": str(job.execution_requests.get().id),
            "manifest_sha256": job.manifest_sha256, "approved": True})
    attempt = Attempt.objects.get(job=job)
    attempt.state = "failed"
    attempt.native_identity = {"job_id": "196068"}
    attempt.observation = {"native_state": "FAILED", "exit_code": "1:0", "source": "sacct"}
    attempt.stderr = "./_broker/environment.sh: line 5: /usr/share/lmod/lmod/init/bash: No such file or directory\n<script>bad()</script>"
    attempt.save(update_fields=["state", "native_identity", "observation", "stderr"])
    client = human(diagnostic.owner)
    page = client.get(f"/review/{job.id}/")
    assert page.status_code == 200
    assert "no-store" in page.headers["Cache-Control"]
    body = page.content.decode()
    assert "Module initialization file is missing" in body and "196068" in body and "1:0" in body
    assert body.index("Module initialization file is missing") < body.index("Exact prepared package")
    assert "&lt;script&gt;bad()&lt;/script&gt;" in body and "<script>bad()</script>" not in body
    assert human(diagnostic.outsider).get(f"/review/{job.id}/").status_code == 403
    test_page = client.get(diagnostic.path)
    assert test_page.status_code == 200
    assert b"Module initialization file is missing" in test_page.content and b"196068" in test_page.content


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_missing_or_unreadable_result_does_not_break_test_page(diagnostic, error):
    config, job = prepared(diagnostic)
    with transaction.atomic():
        services.decide(job, diagnostic.owner, {"execution_request_id": str(job.execution_requests.get().id),
            "manifest_sha256": job.manifest_sha256, "approved": True})
    blob = store_blob(config.project, b'{"marker":"compute-relay-cluster-check-v1"}\n')
    Artifact.objects.create(attempt=Attempt.objects.get(job=job), path="cluster-check.json", blob=blob)
    with patch.object(cluster_checks, "read_blob", side_effect=error("private-path-not-disclosed")):
        page = human(diagnostic.owner).get(diagnostic.path)
    assert page.status_code == 200
    assert b"Stored output could not be verified." in page.content
    assert b"private-path-not-disclosed" not in page.content


def test_account_expiry_recheck_and_forged_resource_fields_cannot_bypass_review(diagnostic, monkeypatch):
    response = form_post(human(diagnostic.owner), diagnostic.path, data(diagnostic, gpus=1, cpus=128,
        approved=True, source="malicious", mode="budgeted"))
    assert response.status_code == 302
    config = ClusterCheckConfiguration.objects.get(connection=diagnostic.connection)
    job = Job.objects.get(project=config.project)
    assert job.request["resources"]["gpus"] == 0 and job.request["resources"]["cpus"] == 2
    assert job.execution_requests.get().state == "awaiting_approval" and not job.attempts.exists()
    monkeypatch.setattr(cluster_checks, "current_user_active", lambda user: False)
    assert human(diagnostic.owner).get(diagnostic.path).status_code == 403
