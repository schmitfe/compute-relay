"""Probe preparation failures remain resumable without weakening job authority."""

from copy import deepcopy
import json
from unittest.mock import patch

from django.test import Client
import pytest

from compute_relay.backends import BackendError
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Attempt, ExecutionRequest, Job
from compute_relay.extensions.environments import inspection, requests
from compute_relay.extensions.environments.models import EnvironmentInspection
from compute_relay.worker.client import strict_json
from test_support.unit_test_environment_extension import context


RESOURCES = {"nodes": 1, "cpus": 2, "gpus": 1, "gpu_type": "h200", "memory_mib": 2048, "time_limit_seconds": 120}


def site(c):
    c.connection.configuration["site_profile"].update(partitions=["gpu", "interactive", "mpi", "smp"],
        accounts=["default"], qos=["normal"], cpu_allocation_quantum=2)
    c.connection.save()
    c.grant.policy.update(allowed_gpu_types=["h200"])
    c.grant.policy["per_job_limits"].update(cpu_cores=4, gpu_devices=1, memory_mib=4096)
    c.grant.policy["allowance"]["gpu_device_seconds"] = 10000
    c.grant.save()


def post(c, body, key):
    return Client(HTTP_AUTHORIZATION="Bearer environment-agent").post(
        f"/api/v1/environments/{c.base.id}/inspect", json.dumps(body), content_type="application/json", HTTP_IDEMPOTENCY_KEY=key)


def test_saved_failed_probe_resumes_with_sole_account_and_qos_preserving_gpu_resources(context):
    c = context
    site(c)
    original = {"resources": RESOURCES, "slurm": {"partition": "gpu"}}
    with patch.object(inspection.core, "prepare_job", side_effect=BackendError("Preparation failed")):
        failed = post(c, original, "initial-probe")
    assert failed.status_code == 400
    current = EnvironmentInspection.objects.get()
    old = current.job
    assert not old.manifest and current.state == "draft"
    # Reproduce the already-saved production record from before this fix.
    legacy = deepcopy(old.request)
    legacy["slurm"] = {"partition": "gpu"}
    Job.objects.filter(id=old.id).update(request=legacy)
    EnvironmentInspection.objects.filter(id=current.id).update(state="prepared")
    inventory = Client(HTTP_AUTHORIZATION="Bearer environment-agent").get(f"/api/v1/environments/{c.base.id}/inventory").json()
    assert inventory["summary"]["pending_inspection"]["state"] == inventory["latest_inspection"]["state"] == "draft"
    result = post(c, {}, "resume-probe")
    assert result.status_code == 200, result.content
    result = strict_json(result.content)
    current.refresh_from_db()
    assert result["id"] == str(current.id)
    assert current.job.previous_id == old.id and current.job_id != old.id
    assert current.job.request["resources"] == RESOURCES
    assert current.job.request["slurm"] == {"partition": "gpu", "account": "default", "qos": "normal"}
    assert current.job.manifest and current.job.extension_name == "environments"
    old.refresh_from_db()
    assert old.request == legacy and old.manifest is None and old.state == "draft"
    # Trusted probe sources remain internal; no upload grant is invented.
    assert set(current.job.files.values_list("source_kind", flat=True)) == {"generated"}
    for body, key in (({}, "resume-probe"), ({}, "another-resume"), (original, "initial-probe")):
        replay = post(c, body, key)
        assert replay.status_code == 200, replay.content
        assert replay.json()["execution"]["id"] == result["execution"]["id"]
        assert replay.json()["execution"]["attempt_id"] == result["execution"]["attempt_id"]
    assert ExecutionRequest.objects.count() == Attempt.objects.count() == 1
    assert Job.objects.count() == 2
    assert not old.execution_requests.exists()
    rejected = post(c, {"resources": {**RESOURCES, "cpus": 4}}, "change-admitted-probe")
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "INSPECTION_ALREADY_REQUESTED"
    assert Job.objects.count() == 2 and Attempt.objects.count() == 1


def test_correcting_resources_creates_an_immutable_revision_after_preparation_failure(context):
    c = context
    site(c)
    c.connection.configuration["site_profile"]["maximum_memory_mib"] = 1024
    c.connection.save()
    failed = post(c, {"resources": RESOURCES, "slurm": {"partition": "gpu"}}, "too-much-memory")
    assert failed.status_code == 409 and failed.json()["error"]["code"] == "RESOURCE_LIMIT"
    current = EnvironmentInspection.objects.get()
    old = current.job
    assert current.state == "draft" and not old.manifest
    corrected = post(c, {"resources": {**RESOURCES, "memory_mib": 1024}}, "fixed-memory")
    assert corrected.status_code == 200, corrected.content
    current.refresh_from_db()
    assert current.job.previous_id == old.id
    assert current.job.request["resources"]["memory_mib"] == 1024
    assert Attempt.objects.count() == 1


def test_ambiguous_scheduler_settings_have_actionable_error_and_no_doomed_job(context):
    c = context
    c.connection.configuration["site_profile"].update(partitions=["debug", "gpu"], accounts=["first", "second"], qos=["normal", "short"])
    c.connection.save()
    for body, key in (({}, "missing-all"), ({"slurm": {"partition": "debug"}}, "missing-account"),
                      ({"slurm": {"partition": "debug", "account": "first"}}, "missing-qos")):
        response = post(c, body, key)
        assert response.status_code == 400 and response.json()["error"]["code"] == "SCHEDULER_OPTIONS_REQUIRED"
        assert not Job.objects.exists() and not ExecutionRequest.objects.exists()
    denied = post(c, {"slurm": {"partition": "debug", "account": "outside", "qos": "normal"}}, "denied-account")
    assert denied.status_code == 400 and denied.json()["error"]["code"] == "SCHEDULER_OPTIONS_DENIED"
    accepted = post(c, {"slurm": {"partition": "debug", "account": "first", "qos": "normal"}}, "choose-scheduler")
    assert accepted.status_code == 200, accepted.content
    assert EnvironmentInspection.objects.count() == Job.objects.count() == Attempt.objects.count() == 1


def test_request_cannot_admit_a_stale_probe_revision(context):
    c = context
    created, _ = inspection.create(c.key, c.base)
    before = EnvironmentInspection.objects.get(id=created["id"])
    inspection.prepare(before)
    after = EnvironmentInspection.objects.get(id=before.id)
    inspection.prepare(after, resources={**after.job.request["resources"], "time_limit_seconds": 120})
    with pytest.raises(BrokerError, match="concurrently"):
        inspection.request_execution(before, c.key)
    assert not Attempt.objects.exists() and not ExecutionRequest.objects.exists()
    assert inspection.request_execution(after, c.key)["state"] == "authorized"
    assert Attempt.objects.count() == 1


def test_probe_requires_execution_scope_before_creating_work(context):
    c = context
    c.key.scopes.remove("jobs:execute")
    c.key.save()
    response = post(c, {}, "no-execute")
    assert response.status_code == 403 and response.json()["error"]["code"] == "SCOPE_DENIED"
    assert not EnvironmentInspection.objects.exists() and not Job.objects.exists()


def test_package_convenience_preserves_unique_scheduler_defaults_with_partial_overrides(context):
    c = context
    c.connection.configuration["site_profile"].update(partitions=["debug", "gpu"], qos=["normal"])
    c.connection.save()
    document = requests.package_recipe(c.key, c.base, {"packages": [{"name": "example", "version": "1.0",
        "filename": "example-1.0-py3-none-any.whl", "sha256": "a" * 64, "size_bytes": 100}], "slurm": {"partition": "debug"}})
    assert document["slurm"] == {"partition": "debug", "account": "broker", "qos": "normal"}


def test_closed_failed_probe_cannot_be_resurrected_by_replaying_its_key(context):
    c = context
    with patch.object(inspection.core, "prepare_job", side_effect=BackendError("Preparation failed")):
        assert post(c, {}, "closed-probe").status_code == 400
    closed = EnvironmentInspection.objects.get()
    Job.objects.filter(id=closed.job_id).update(state="cancelled")
    replay = post(c, {}, "closed-probe")
    assert replay.status_code == 409 and replay.json()["error"]["code"] == "INSPECTION_CLOSED"
    closed.job.refresh_from_db()
    assert closed.job.state == "cancelled" and not Attempt.objects.exists()
    fresh = post(c, {}, "fresh-probe")
    assert fresh.status_code == 200 and fresh.json()["id"] != str(closed.id)
    assert Attempt.objects.count() == 1


def test_reused_project_idempotency_key_does_not_cross_inspection_credential(context):
    from compute_relay.control.auth import token_hash
    from compute_relay.control.models import ProjectCredential
    c = context
    first = post(c, {}, "shared-key")
    assert first.status_code == 200, first.content
    ProjectCredential.objects.create(project=c.project, issued_by=c.owner, name="other", prefix="other",
        token_hash=token_hash("other-agent"), scopes=c.key.scopes, expires_at=c.key.expires_at)
    other = Client(HTTP_AUTHORIZATION="Bearer other-agent").post(f"/api/v1/environments/{c.base.id}/inspect", "{}",
        content_type="application/json", HTTP_IDEMPOTENCY_KEY="shared-key")
    assert other.status_code == 404
    assert EnvironmentInspection.objects.count() == Job.objects.count() == Attempt.objects.count() == 1
