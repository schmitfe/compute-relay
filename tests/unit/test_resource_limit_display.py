"""Current resource eligibility remains visible before an execution exists.

Packages, accounts and cluster configuration here are synthetic; these checks do
not connect to a worker or submit any scheduler jobs.
"""

from django.template import Context, Template
from django.test import Client

from compute_relay.control.models import Connection
from compute_relay.control.templatetags.broker_resource_limits import effective_resource_rows
from test_control import broker, prepared_job


def owner_client(context):
    client = Client()
    client.force_login(context.owner)
    return client


def shrink_cpu_envelope(context, *, mode="budgeted"):
    context.grant.policy = {**context.grant.policy, "mode": mode,
        "per_job_limits": {**context.grant.policy["per_job_limits"], "cpu_cores": 2}}
    context.grant.save(update_fields=["policy"])


def test_prepared_job_without_attempt_displays_current_resource_failure_and_concrete_limits(broker):
    job = prepared_job(broker, cpus=4)
    shrink_cpu_envelope(broker)
    response = owner_client(broker).get(f"/review/{job.id}/")
    assert response.status_code == 200
    text = response.content.decode()
    assert 'id="resource-validation"' in text and 'data-resource-valid="false"' in text
    assert "The prepared package exceeds current resource limits" in text
    assert "Effective allocation" in text and "Limit source" in text
    assert "4 cores" in text and "2 cores" in text
    assert "Approval does not bypass this project" in text
    assert not job.attempts.exists()
    assert response["Cache-Control"] == "no-store, private"


def test_review_describes_mixed_policy_human_exception_without_granting_it(broker):
    job = prepared_job(broker, cpus=4)
    shrink_cpu_envelope(broker, mode="mixed")
    response = owner_client(broker).get(f"/review/{job.id}/")
    text = response.content.decode()
    assert "approve an exception to its per-job envelope" in text
    assert "Cluster limits, available compute allowance" in text
    assert not job.attempts.exists() and not job.execution_requests.exists()


def test_review_resource_success_does_not_claim_execution_authorization(broker):
    job = prepared_job(broker)
    text = owner_client(broker).get(f"/review/{job.id}/").content.decode()
    assert 'data-resource-valid="true"' in text
    assert "fits the current project resource envelope" in text
    assert "all other execution checks still apply" in text
    assert not job.attempts.exists()


def test_project_shows_effective_limits_without_disclosing_another_members_cluster(broker):
    broker.connection.configuration["site_profile"] = {
        "maximum_cpus": 2, "maximum_gpus": 0, "maximum_memory_mib": 256,
        "maximum_time_limit_seconds": 600, "allocated_cpus": 2,
    }
    broker.connection.save(update_fields=["configuration"])
    foreign = Connection.objects.create(owner=broker.outsider, worker=broker.worker,
        name="PRIVATE-OTHER-MEMBER-CLUSTER", backend="fake",
        configuration={"site_profile": {"maximum_cpus": 88}, "private": "PRIVATE-CONFIG"},
        revision_sha256="a" * 64)
    broker.project.connections.add(foreign)
    broker.grant.policy["allowed_connection_ids"].append(str(foreign.id))
    broker.grant.save(update_fields=["policy"])
    response = owner_client(broker).get(f"/projects/{broker.project.id}/")
    assert response.status_code == 200
    text = response.content.decode()
    assert "Current per-job limits by cluster" in text
    assert "Project ceiling" in text and "Cluster ceiling" in text and "Effective ceiling" in text
    assert "<strong>256</strong>" in text and "<strong>600</strong>" in text
    assert "PRIVATE-" not in text
    assert "charge_minutes" not in text and "Maximum charged duration" not in text


def test_limit_table_selects_numeric_public_fields_and_preserves_zero_gpu_ceiling():
    rows = effective_resource_rows({
        "per_job_limits": {"cpu_cores": 2, "gpu_devices": 0, "memory_mib": 256, "time_limit_seconds": 600, "secret": "PRIVATE"},
        "project_limits": {"cpu_cores": True}, "cluster_limits": {"maximum_cpus": "PRIVATE"},
        "configuration": {"private": "PRIVATE"},
    })
    assert rows[0]["project"] is None and rows[0]["cluster"] is None
    assert rows[1]["effective"] == 0
    assert "PRIVATE" not in str(rows)


def test_resource_check_escapes_values_even_when_parent_disables_autoescape():
    validation = {"valid": False, "violations": [{
        "resource": "cpu_cores", "requested": 1, "effective": 4, "limit": 2,
        "excess": 2, "source": "project", "unit": '<img src=x onerror="alert(1)">',
    }]}
    text = Template('{% autoescape off %}{% include "broker/resource_validation.html" %}{% endautoescape %}').render(
        Context({"validation": validation}))
    assert "<img" not in text and "&lt;img" in text
    assert "CPUs" in text and "Project" in text
