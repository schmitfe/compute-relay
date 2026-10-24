"""Recipe identities belong to one cluster; historical accounting stays usable."""

import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import transaction
from django.test import Client
from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.auth import token_hash
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import (
    ApprovalDecision, Attempt, Connection, Environment, ExecutionRequest, Grant,
    Job, Project, ProjectCredential, User, WorkerIdentity,
)
from compute_relay.control.storage import store_blob


@pytest.fixture
def catalog(db, settings, tmp_path):
    settings.ARTIFACT_ROOT = tmp_path / "objects"
    owner = User.objects.create_user(username="cluster-recipe-owner")
    worker = WorkerIdentity.objects.create(owner=owner, name="synthetic", token_hash=token_hash("synthetic-worker"))
    targets = [Connection.objects.create(owner=owner, worker=worker, name=name, backend="fake",
                                         configuration={"site_profile": {"automatic_execution_allowed": True}}, revision_sha256=revision * 64)
               for name, revision in [("internal", "a"), ("university", "b")]]
    configuration = {"interpreter": "/usr/bin/python3"}
    revision = services.digest(configuration)
    environments = [Environment.objects.create(owner=owner, connection=target, name="Python", configuration=configuration,
                                                revision_sha256=revision) for target in targets]
    project = Project.objects.create(owner=owner, name="cluster-bound recipes")
    project.connections.add(*targets)
    project.environments.add(*environments)
    token = "synthetic-catalog-project"
    credential = ProjectCredential.objects.create(project=project, issued_by=project.owner, name="catalog agent", prefix="catalog",
        token_hash=token_hash(token), scopes=["jobs:read", "jobs:write", "jobs:execute"],
        expires_at=timezone.now() + timedelta(days=1))
    policy = {"schema_version": "1.0", "project_id": str(project.id), "revision": 1, "mode": "budgeted",
              "allowed_connection_ids": [str(target.id) for target in targets],
              "allowed_environment_revision_sha256": [revision], "allowed_gpu_types": [],
              "per_job_limits": {"cpu_cores": 4, "gpu_devices": 0, "memory_mib": 512,
                                 "time_limit_seconds": 600},
              "max_active_jobs": 10, "allowance": {"cpu_core_seconds": 10000, "gpu_device_seconds": 0},
              "admission_expires_at": (timezone.now() + timedelta(days=1)).isoformat()}
    grant = Grant.objects.create(project=project, policy=policy)
    blob = store_blob(project, b"print('this upload is never executed by the test')\n")
    request = {"schema_version": "1.0", "project_id": str(project.id), "connection_id": str(targets[0].id),
               "name": "bound-recipe", "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
               "environment": {"id": str(environments[0].id), "revision_sha256": revision},
               "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
               "slurm": {"partition": "debug", "account": "broker"},
               "files": [{"path": "main.py", "sha256": blob.sha256, "size_bytes": blob.size_bytes, "executable": False}],
               "outputs": []}
    return SimpleNamespace(owner=owner, worker=worker, targets=targets, environments=environments, project=project,
                           credential=credential, token=token, request=request, grant=grant)


def _job(catalog, *, prepared=True):
    created, _ = services.create_job(catalog.credential, copy.deepcopy(catalog.request))
    job = Job.objects.get(id=created["id"])
    if prepared:
        services.prepare_job(job)
        job.refresh_from_db()
    return job


def _execute(catalog, job):
    with transaction.atomic():
        result, _ = services.request_execution(job, catalog.credential)
    return ExecutionRequest.objects.get(id=result["id"])


def test_identical_interpreter_and_digest_do_not_make_recipe_portable(catalog):
    assert catalog.environments[0].configuration == catalog.environments[1].configuration
    assert catalog.environments[0].revision_sha256 == catalog.environments[1].revision_sha256
    request = copy.deepcopy(catalog.request)
    request["connection_id"] = str(catalog.targets[1].id)
    with pytest.raises(BrokerError) as denied:
        services.create_job(catalog.credential, request)
    assert denied.value.code == "REFERENCE_DENIED"
    request["environment"]["id"] = str(catalog.environments[1].id)
    result, status = services.create_job(catalog.credential, request)
    assert status == 201
    assert Job.objects.get(id=result["id"]).request["connection_id"] == str(catalog.targets[1].id)




def test_inventory_exposes_only_recipes_for_effective_allowed_clusters(catalog):
    disabled = Connection.objects.create(owner=catalog.owner, worker=catalog.worker, name="disabled", backend="fake",
                                          configuration={}, revision_sha256="d" * 64, enabled=False)
    disabled_recipe = Environment.objects.create(owner=catalog.owner, connection=disabled, name="disabled Python",
                                                  configuration={}, revision_sha256="f" * 64)
    catalog.project.connections.add(disabled)
    catalog.project.environments.add(disabled_recipe)
    catalog.credential.allowed_connection_ids = [str(catalog.targets[0].id), str(disabled.id)]
    catalog.credential.save(update_fields=["allowed_connection_ids"])
    response = Client().get("/api/v1/connections", HTTP_AUTHORIZATION="Bearer " + catalog.token)
    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["connections"]] == [str(catalog.targets[0].id)]
    assert payload["environments"] == [{"id": str(catalog.environments[0].id), "name": "Python",
                                        "revision_sha256": catalog.environments[0].revision_sha256,
                                        "connection_id": str(catalog.targets[0].id)}]


@pytest.mark.parametrize("stage", ["prepare", "request", "approve", "dispatch"])
def test_recipe_binding_is_rechecked_before_each_new_execution_stage(catalog, stage):
    job = _job(catalog, prepared=stage != "prepare")
    execution = None
    if stage == "approve":
        catalog.grant.policy = {**catalog.grant.policy, "mode": "approval_required"}
        catalog.grant.save(update_fields=["policy"])
        execution = _execute(catalog, job)
    elif stage == "dispatch":
        execution = _execute(catalog, job)
        lease = services.lease_work(catalog.worker)["lease"]
    # Changing a recipe's cluster does not rewrite any already sealed bytes.
    Environment.objects.filter(id=catalog.environments[0].id).update(connection=catalog.targets[1])
    with pytest.raises(BrokerError) as denied:
        with transaction.atomic():
            if stage == "prepare":
                services.prepare_job(job)
            elif stage == "request":
                services.request_execution(job, catalog.credential)
            elif stage == "approve":
                services.decide(job, catalog.owner, {"execution_request_id": str(execution.id), "approved": True,
                                                     "manifest_sha256": job.manifest_sha256})
            else:
                services.dispatch(catalog.worker, execution.attempt.id,
                                  {"lease_id": lease["id"], "generation": lease["generation"],
                                   "manifest_sha256": job.manifest_sha256,
                                   "connection_revision_sha256": catalog.targets[0].revision_sha256})
    assert denied.value.code == "REFERENCE_DENIED"
    assert not ApprovalDecision.objects.exists()
    if job.manifest:
        package = services.serialize_job(job, package=True)
        assert package["manifest_sha256"] == job.manifest_sha256
        assert services.prepare_job(job)[0]["manifest_sha256"] == job.manifest_sha256
    if stage == "dispatch":
        execution.attempt.refresh_from_db()
        assert not execution.attempt.dispatch_started


def test_recipe_rebinding_does_not_block_observation_or_final_accounting(catalog):
    job = _job(catalog)
    execution = _execute(catalog, job)
    attempt = execution.attempt
    lease = services.lease_work(catalog.worker)["lease"]
    identity = {"job_id": "17", "attempt_id": str(attempt.id), "cluster": "simulation"}
    services.dispatch(catalog.worker, attempt.id,
                      {"lease_id": lease["id"], "generation": lease["generation"],
                       "manifest_sha256": job.manifest_sha256,
                       "connection_revision_sha256": catalog.targets[0].revision_sha256})
    services.report(catalog.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
                                                "outcome": "submitted", "native_identity": identity})
    Environment.objects.filter(id=catalog.environments[0].id).update(connection=catalog.targets[1])
    Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now() - timedelta(seconds=1))
    observation_lease = services.lease_work(catalog.worker)["lease"]
    assert observation_lease["operation"] == "observe"
    services.report(catalog.worker, attempt.id,
                    {"lease_id": observation_lease["id"], "generation": observation_lease["generation"], "outcome": "observed",
                     "observation": {"state": "completed", "native_state": "COMPLETED", "terminal": True,
                                     "accounting": {"cpu_core_seconds": 2, "gpu_device_seconds": 0, "final": True}}})
    attempt.refresh_from_db()
    catalog.grant.refresh_from_db()
    assert attempt.accounting_state == "settled"
    assert catalog.grant.spent_cpu == 2
    assert catalog.grant.reserved_cpu == 0
