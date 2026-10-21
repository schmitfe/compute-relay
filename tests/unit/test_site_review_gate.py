"""An unverified site envelope cannot grant unattended execution authority.

The prepared package below is synthetic and exercises only admission/dispatch
decisions. No backend, SSH command, executable payload, or cluster is invoked.
"""

import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import transaction
from django.utils import timezone

from compute_broker.control import services
from compute_broker.control.auth import token_hash
from compute_broker.control.cluster_services import publish_profiles
from compute_broker.control.errors import BrokerError
from compute_broker.control.models import (
    ApprovalDecision, Attempt, Connection, Environment, ExecutionRequest, Grant, Job,
    LedgerEntry, Project, ProjectCredential, User, WorkerIdentity,
)
from compute_broker.worker.enrollment import profile_metadata


@pytest.fixture
def review_site(db):
    owner = User.objects.create_user(username="site-review-owner", password=None)
    worker = WorkerIdentity.objects.create(owner=owner, name="site-review-worker",
                                           token_hash=token_hash("synthetic-site-review-worker"))
    connection = Connection.objects.create(owner=owner, worker=worker, name="unverified simulation", backend="fake",
                                           configuration={"site_profile": {"automatic_execution_allowed": False}},
                                           revision_sha256="c" * 64)
    project = Project.objects.create(owner=owner, name="site-review-project")
    project.connections.add(connection)
    environment = Environment.objects.create(owner=owner, connection=connection, name="synthetic site Python",
        configuration={"interpreter": "/usr/bin/python3"}, revision_sha256="e" * 64)
    project.environments.add(environment)
    credential = ProjectCredential.objects.create(project=project, name="automatic-capable key", prefix="synthetic",
        token_hash=token_hash("synthetic-site-review-key"), execution_mode="automatic",
        scopes=["jobs:write", "jobs:execute", "jobs:read"], expires_at=timezone.now() + timedelta(days=1))
    policy = {"schema_version": "1.0", "project_id": str(project.id), "revision": 1, "mode": "budgeted",
              "allowed_connection_ids": [str(connection.id)], "allowed_environment_revision_sha256": ["e" * 64],
              "allowed_gpu_types": [], "max_active_jobs": 10, "max_total_jobs": 10,
              "per_job_limits": {"cpu_cores": 1, "gpu_devices": 0, "memory_mib": 128,
                                 "time_limit_seconds": 60, "maximum_charge_duration_seconds": 60},
              "allowance": {"cpu_core_seconds": 1000, "gpu_device_seconds": 0},
              "admission_expires_at": (timezone.now() + timedelta(days=1)).isoformat()}
    grant = Grant.objects.create(project=project, policy=policy)
    job = Job.objects.create(project=project, credential=credential, state="prepared",
        request={"connection_id": str(connection.id), "name": "supervised-site-test", "outputs": [],
                 "environment": {"id": str(environment.id), "revision_sha256": "e" * 64}, "resources": {"memory_mib": 128}},
        manifest={"connection_revision_sha256": "c" * 64,
                  "maximum_allocation": {"cpu_cores": 1, "gpu_devices": 0, "time_limit_seconds": 60,
                                         "maximum_charge_duration_seconds": 60},
                  "reservation": {"cpu_core_seconds": 60, "gpu_device_seconds": 0}}, manifest_sha256="f" * 64)
    return SimpleNamespace(owner=owner, worker=worker, connection=connection, project=project,
                           credential=credential, grant=grant, job=job)


def request_execution(context):
    with transaction.atomic():
        result, _ = services.request_execution(context.job, context.credential)
    return result, ExecutionRequest.objects.get(id=result["id"])


def approve(context, execution):
    with transaction.atomic():
        return services.decide(context.job, context.owner, {"execution_request_id": str(execution.id),
            "manifest_sha256": context.job.manifest_sha256, "approved": True, "comment": "Supervised synthetic test."})


def dispatch(context, attempt):
    lease = services.lease_work(context.worker)["lease"]
    assert lease["attempt_id"] == str(attempt.id)
    return services.dispatch(context.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "manifest_sha256": context.job.manifest_sha256, "connection_revision_sha256": context.connection.revision_sha256})


@pytest.mark.parametrize("project_mode", ["budgeted", "mixed"])
@pytest.mark.parametrize("key_mode", ["inherit", "automatic"])
def test_unverified_site_always_waits_for_review_without_consuming_compute_or_job_slots(review_site, project_mode, key_mode):
    context = review_site
    context.grant.policy = {**context.grant.policy, "mode": project_mode}
    context.grant.save(update_fields=["policy"])
    context.credential.execution_mode = key_mode
    context.credential.save(update_fields=["execution_mode"])
    result, execution = request_execution(context)
    assert result["attempt_id"] is None
    assert execution.state == "awaiting_approval"
    assert not Attempt.objects.exists()
    assert not LedgerEntry.objects.exists()
    allowance = services.allowance(context.project)
    assert allowance["reserved"] == {"cpu_core_seconds": 0, "gpu_device_seconds": 0}
    assert allowance["spent"] == {"cpu_core_seconds": 0, "gpu_device_seconds": 0}
    assert allowance["jobs"]["admitted"] == 0


def test_direct_authorization_cannot_skip_site_review_gate(review_site):
    context = review_site
    execution = ExecutionRequest.objects.create(job=context.job, credential=context.credential)
    with pytest.raises(BrokerError) as denied:
        with transaction.atomic():
            services.authorize(execution)
    assert (denied.value.code, denied.value.status) == ("PROFILE_REVIEW_REQUIRED", 409)
    assert not Attempt.objects.exists()
    context.grant.refresh_from_db()
    assert context.grant.reserved_cpu == context.grant.reserved_gpu == 0


def test_exact_human_approval_can_authorize_and_dispatch_unverified_site_once(review_site):
    context = review_site
    _, execution = request_execution(context)
    approve(context, execution)
    attempt = Attempt.objects.get(execution_request=execution)
    decision = ApprovalDecision.objects.get(execution_request=execution)
    assert decision.approved and decision.consumed_at is not None
    assert decision.manifest_sha256 == context.job.manifest_sha256
    assert dispatch(context, attempt) == {"authorized": True}
    attempt.refresh_from_db()
    assert attempt.dispatch_started
    next_result, _ = request_execution(context)
    assert next_result["attempt_id"] is None
    assert Attempt.objects.count() == 1


def test_dispatch_requires_approval_even_for_an_existing_automatic_authorization(review_site):
    context = review_site
    # Model an authorization admitted before the operator disabled automation.
    # Keep the synthetic revision constant to isolate this final dispatch gate.
    context.connection.configuration = {"site_profile": {"automatic_execution_allowed": True}}
    context.connection.save(update_fields=["configuration"])
    result, execution = request_execution(context)
    assert result["attempt_id"]
    attempt = Attempt.objects.get(execution_request=execution)
    context.connection.configuration = {"site_profile": {"automatic_execution_allowed": False}}
    context.connection.save(update_fields=["configuration"])
    with pytest.raises(BrokerError) as denied:
        dispatch(context, attempt)
    assert denied.value.status == 403
    attempt.refresh_from_db()
    assert not attempt.dispatch_started
    assert not ApprovalDecision.objects.exists()


@pytest.mark.parametrize("site_profile", [{}, {"automatic_execution_allowed": True}])
def test_legacy_or_explicitly_verified_site_retains_budgeted_automation(review_site, site_profile):
    context = review_site
    context.connection.configuration = {"site_profile": site_profile}
    context.connection.save(update_fields=["configuration"])
    result, _ = request_execution(context)
    assert result["attempt_id"]
    assert Attempt.objects.count() == 1


def advertised_profile(automatic_execution_allowed=False):
    return profile_metadata("synthetic-site", {
        "backend": "fake", "allowed_hosts": ["fixture.invalid"], "allowed_users": ["fixtureuser"],
        "allowed_ports": [22], "remote_root_prefixes": ["/simulated/jobs"], "profile_revision": 1,
        "site_profile": {"partitions": ["debug"], "accounts": ["broker"], "qos": [],
                         "automatic_execution_allowed": automatic_execution_allowed},
    })


@pytest.mark.parametrize("allowed", [False, True])
def test_control_preserves_strict_boolean_site_review_metadata(review_site, allowed):
    context = review_site
    metadata = advertised_profile(allowed)
    assert publish_profiles(context.worker, {"profiles": [metadata]}) == {"accepted": True}
    context.worker.refresh_from_db()
    assert context.worker.profiles[0]["site_profile"]["automatic_execution_allowed"] is allowed


@pytest.mark.parametrize("invalid", [0, 1, "false", "true", None, [], {}])
def test_control_rejects_nonboolean_site_review_metadata_without_replacing_last_good_profile(review_site, invalid):
    context = review_site
    original = advertised_profile(False)
    publish_profiles(context.worker, {"profiles": [original]})
    bad = copy.deepcopy(original)
    bad["site_profile"]["automatic_execution_allowed"] = invalid
    with pytest.raises(BrokerError) as rejected:
        publish_profiles(context.worker, {"profiles": [bad]})
    assert rejected.value.code == "INVALID_PROFILES"
    context.worker.refresh_from_db()
    assert context.worker.profiles == [original]
