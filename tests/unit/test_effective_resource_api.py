"""Authenticated planning reflects the active envelope before an agent retries.

All limits, accounts and prepared packages are synthetic. No connector or real
scheduler is contacted, and no submitted source is executed.
"""

from copy import deepcopy
import json

from django.test import Client

from compute_broker.control import services
from compute_broker.control.models import Attempt, Connection, Environment, ExecutionRequest, Job, LedgerEntry
from test_control import broker, prepared_job


def client(context):
    return Client(HTTP_AUTHORIZATION="Bearer " + context.token)


def profile(context, **changes):
    current = {"maximum_cpus": 16, "maximum_gpus": 4, "maximum_memory_mib": 4096,
        "maximum_time_limit_seconds": 7200, "allocated_cpus": 4, "allocated_gpus": 0,
        "partitions": ["debug"], "accounts": ["broker"], "qos": [],
        "termination_margin_seconds": 900, "overtime_margin_seconds": 800,
        "cleanup_margin_seconds": 700, "private": "PRIVATE-CLUSTER-CONFIG", **changes}
    context.connection.configuration["site_profile"] = current
    context.connection.save(update_fields=["configuration"])


def policy(context, **changes):
    context.grant.policy = {**context.grant.policy, **changes}
    context.grant.save(update_fields=["policy"])


def resource_revision(context, **resources):
    baseline = prepared_job(context)
    request = deepcopy(baseline.request)
    request["name"] = "synthetic-resource-revision"
    request["resources"].update(resources)
    if request["resources"]["gpus"]:
        request["resources"]["gpu_type"] = "a100"
    result, status = services.create_job(context.credential, request)
    assert status == 201
    return Job.objects.get(id=result["id"])


def no_admission(context):
    context.grant.refresh_from_db()
    assert context.grant.spent_cpu == context.grant.reserved_cpu == 0
    assert context.grant.spent_gpu == context.grant.reserved_gpu == 0
    assert not Attempt.objects.exists() and not LedgerEntry.objects.exists()
    assert services.allowance(context.project)["jobs"]["admitted"] == 0


def test_targets_advertise_project_cluster_minimums_and_allocation_floors_without_legacy_fields(broker):
    profile(broker, maximum_cpus=6, maximum_gpus=0, maximum_memory_mib=1024, maximum_time_limit_seconds=600)
    response = client(broker).get("/api/v1/connections")
    assert response.status_code == 200
    data = response.json()
    target = data["connections"][0]
    assert target["effective_limits"]["per_job_limits"] == {
        "nodes": 1, "cpu_cores": 6, "gpu_devices": 0, "memory_mib": 512, "time_limit_seconds": 600}
    assert target["effective_limits"]["allocation_floors"] == {"cpu_cores": 4, "gpu_devices": 0}
    assert target["effective_limits"]["feasible"] is True
    assert data["per_job_limits"] == {
        "cpu_cores": 8, "gpu_devices": 1, "memory_mib": 512, "time_limit_seconds": 3600}
    text = response.content.decode()
    assert "margin_seconds" not in text and "maximum_charge_duration_seconds" not in text
    assert "PRIVATE-" not in text and broker.token not in text


def test_targets_refresh_active_policy_and_report_allocation_floor_that_no_longer_fits(broker):
    profile(broker)
    before = client(broker).get("/api/v1/connections").json()
    assert before["connections"][0]["effective_limits"]["per_job_limits"]["cpu_cores"] == 8
    policy(broker, revision=2, per_job_limits={**broker.grant.policy["per_job_limits"], "cpu_cores": 2})
    after = client(broker).get("/api/v1/connections").json()
    limits = after["connections"][0]["effective_limits"]
    assert after["policy_revision"] == 2 and limits["per_job_limits"]["cpu_cores"] == 2
    assert limits["allocation_floors"]["cpu_cores"] == 4
    assert limits["feasible"] is False and limits["blocking_reasons"]
    no_admission(broker)


def additional_target(context, name, *, owner=None, digest="a"):
    connection = Connection.objects.create(owner=owner or context.owner, worker=context.worker,
        backend="fake", name=name, revision_sha256=digest * 64, configuration={})
    environment = Environment.objects.create(owner=owner or context.owner, connection=connection,
        name=name + "-environment", revision_sha256=digest * 64, configuration={"interpreter": "/usr/bin/python3"})
    context.project.connections.add(connection)
    context.project.environments.add(environment)
    return connection, environment


def test_active_grant_filters_targets_and_environment_revisions_then_key_can_only_narrow(broker):
    other, other_environment = additional_target(broker, "second-owned")
    forbidden, forbidden_environment = additional_target(broker, "PRIVATE-FOREIGN-CLUSTER", owner=broker.outsider, digest="b")
    policy(broker, allowed_connection_ids=[str(other.id), str(forbidden.id)],
        allowed_environment_revision_sha256=[other_environment.revision_sha256, forbidden_environment.revision_sha256])
    response = client(broker).get("/api/v1/connections")
    data = response.json()
    assert [target["id"] for target in data["connections"]] == [str(other.id)]
    assert [environment["id"] for environment in data["environments"]] == [str(other_environment.id)]
    assert "PRIVATE-" not in response.content.decode()

    broker.credential.allowed_connection_ids = [str(broker.connection.id)]
    broker.credential.save(update_fields=["allowed_connection_ids"])
    denied = client(broker).get("/api/v1/connections").json()
    assert denied["connections"] == denied["environments"] == []

    broker.credential.allowed_connection_ids = [str(other.id)]
    broker.credential.save(update_fields=["allowed_connection_ids"])
    policy(broker, allowed_environment_revision_sha256=[])
    narrowed = client(broker).get("/api/v1/connections").json()
    assert [target["id"] for target in narrowed["connections"]] == [str(other.id)]
    assert narrowed["environments"] == []


def test_context_and_allowance_share_current_compute_payload_with_targets(broker):
    profile(broker)
    api = client(broker)
    targets = api.get("/api/v1/connections").json()
    context = api.get("/api/v1/context").json()
    allowance = api.get(f"/api/v1/projects/{broker.project.id}/allowance").json()
    assert targets.pop("as_of") and context["compute"].pop("as_of") and allowance["compute"].pop("as_of")
    assert targets == context["compute"] == allowance.pop("compute")
    assert context["allowance"] == allowance == services.allowance(broker.project)


def test_context_without_job_read_scope_reveals_only_key_identity_not_compute_or_allowance(broker):
    broker.credential.scopes = ["objects:write"]
    broker.credential.save(update_fields=["scopes"])
    api = client(broker)
    data = api.get("/api/v1/context").json()
    assert set(data) == {"schema_version", "project", "credential"}
    assert api.get("/api/v1/connections").status_code == 403
    assert api.get(f"/api/v1/projects/{broker.project.id}/allowance").status_code == 403
    no_admission(broker)


def test_project_envelope_failure_reports_every_exceeded_resource_without_consuming_admission(broker):
    job = resource_revision(broker, cpus=4, gpus=2, memory_mib=1024, time_limit_seconds=120)
    services.prepare_job(job)
    policy(broker, mode="budgeted", per_job_limits={
        "cpu_cores": 2, "gpu_devices": 1, "memory_mib": 512, "time_limit_seconds": 60})
    response = client(broker).post(f"/api/v1/jobs/{job.id}/execution-requests", data="{}",
        content_type="application/json", HTTP_IDEMPOTENCY_KEY="synthetic-resource-denial")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "RESOURCE_LIMIT"
    rows = {item["resource"]: item for item in error["details"]["violations"]}
    assert set(rows) == {"cpu_cores", "gpu_devices", "memory_mib", "time_limit_seconds"}
    assert (rows["cpu_cores"]["requested"], rows["cpu_cores"]["effective"], rows["cpu_cores"]["limit"], rows["cpu_cores"]["excess"]) == (4, 4, 2, 2)
    assert all(item["source"] == "project" for item in rows.values())
    assert all(word in error["message"] for word in ("CPU allocation", "GPU allocation", "Memory", "Walltime"))
    assert not ExecutionRequest.objects.exists()
    no_admission(broker)


def test_project_error_distinguishes_requested_cpu_count_from_cluster_allocation_floor(broker):
    profile(broker)
    job = prepared_job(broker, cpus=1)
    policy(broker, mode="budgeted", per_job_limits={
        **broker.grant.policy["per_job_limits"], "cpu_cores": 2})
    response = client(broker).post(f"/api/v1/jobs/{job.id}/execution-requests", data="{}",
        content_type="application/json", HTTP_IDEMPOTENCY_KEY="synthetic-rounded-denial")
    assert response.status_code == 409
    row, = response.json()["error"]["details"]["violations"]
    assert (row["resource"], row["requested"], row["effective"], row["limit"], row["excess"]) == (
        "cpu_cores", 1, 4, 2, 2)
    no_admission(broker)


def test_cluster_prepare_failure_reports_all_caps_before_creating_prepared_package_or_reservation(broker):
    job = resource_revision(broker, cpus=4, gpus=2, memory_mib=1024, time_limit_seconds=120)
    profile(broker, maximum_cpus=2, maximum_gpus=1, maximum_memory_mib=512,
        maximum_time_limit_seconds=60, allocated_cpus=1)
    response = client(broker).post(f"/api/v1/jobs/{job.id}/prepare", data="{}",
        content_type="application/json", HTTP_IDEMPOTENCY_KEY="synthetic-cluster-denial")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "RESOURCE_LIMIT"
    assert {item["resource"] for item in error["details"]["violations"]} == {
        "cpu_cores", "gpu_devices", "memory_mib", "time_limit_seconds"}
    assert all(item["source"] == "cluster" for item in error["details"]["violations"])
    assert "PRIVATE-" not in json.dumps(error)
    job.refresh_from_db()
    assert not job.manifest and job.state != "prepared"
    assert not ExecutionRequest.objects.exists()
    no_admission(broker)
