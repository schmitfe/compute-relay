"""Current, credential-filtered planning information and resource diagnostics."""

from django.utils import timezone

from compute_relay.domain.resource_limits import allocation_violations, effective_resource_limits
from compute_relay.domain.allocation import allocated_cpus

from .auth import connection_allowed
from .connection_identity import public_ssh_identity
from .execution_policy import approval_requirements
from .models import Connection, Grant


PROJECT_RESOURCES = ("cpu_cores", "gpu_devices", "memory_mib", "time_limit_seconds")
SITE_RESOURCES = dict(zip(PROJECT_RESOURCES,
    ("maximum_cpus", "maximum_gpus", "maximum_memory_mib", "maximum_time_limit_seconds")))
RESOURCE_LABELS = {"cpu_cores": "CPU allocation", "gpu_devices": "GPU allocation",
    "memory_mib": "Memory", "time_limit_seconds": "Walltime"}
PUBLIC_PROFILE_FIELDS = {"partitions", "accounts", "qos", "allocated_cpus", "allocated_gpus", "cpu_allocation_quantum",
    "automatic_execution_allowed", *SITE_RESOURCES.values()}


def project_limits(policy):
    return {field: policy.get("per_job_limits", {}).get(field) for field in PROJECT_RESOURCES}


def compute_context(credential):
    """Use current project policy plus only this key's usable cluster bindings."""
    project = credential.project
    grant = Grant.objects.filter(project=project).first()
    if grant is None:
        return {"policy_revision": None, "per_job_limits": {}, "connections": [], "environments": [],
                "planning_state": "project_policy_missing"}
    policy = grant.policy
    allowed = set(policy.get("allowed_connection_ids", []))
    targets = [connection for connection in project.connections.filter(enabled=True).select_related("worker")
               if str(connection.id) in allowed and connection_allowed(credential, connection.id)]
    target_ids = {connection.id for connection in targets}
    environments = project.environments.filter(connection_id__in=target_ids,
        revision_sha256__in=policy.get("allowed_environment_revision_sha256", [])).order_by("name", "id")
    return {
        "policy_revision": policy.get("revision"),
        "per_job_limits": project_limits(policy),
        "project_mode": policy.get("mode"), "project_paused": project.paused,
        "admission_expires_at": policy.get("admission_expires_at"),
        "key_execution_mode": credential.execution_mode,
        "allowed_gpu_types": policy.get("allowed_gpu_types", []),
        "as_of": timezone.now().isoformat(),
        "connections": [{"id": str(connection.id), "name": connection.name, "backend": connection.backend,
            "ssh_identity": public_ssh_identity(connection.configuration) if connection.backend == "slurm" else None,
            "auth_state": connection.auth_state,
            "capabilities": {field: value for field, value in connection.configuration.get("site_profile", {}).items()
                             if field in PUBLIC_PROFILE_FIELDS},
            "effective_limits": effective_resource_limits(policy, connection.configuration, backend=connection.backend),
            **approval_requirements(policy, credential.execution_mode, connection.configuration),
            "connection_revision_sha256": connection.revision_sha256,
        } for connection in targets],
        "environments": [{"id": str(environment.id), "name": environment.name,
            "revision_sha256": environment.revision_sha256, "connection_id": str(environment.connection_id)}
            for environment in environments],
    }


def violations_for_connection(resources, allocation, configuration):
    """Only configured site caps are authoritative; saved discovery is evidence."""
    profile = configuration.get("site_profile", {})
    # Absent site caps add no restriction in the existing preparation contract.
    limits = {resource: profile.get(site_field, 2**53 - 1) for resource, site_field in SITE_RESOURCES.items()}
    return [{**violation, "source": "cluster"}
            for violation in allocation_violations(resources, allocation, limits)]


def planned_allocation(resources, configuration):
    profile = configuration.get("site_profile", {})
    return {"cpu_cores": allocated_cpus(resources["cpus"], profile),
            "gpu_devices": max(resources["gpus"], profile.get("allocated_gpus", resources["gpus"])),
            "time_limit_seconds": resources["time_limit_seconds"]}


def job_approval_requirements(job, *, credential=None, policy=None, connection=None):
    """Explain current review settings for an already-authorized job reader."""
    if policy is None:
        grant = Grant.objects.filter(project_id=job.project_id).first()
        if grant is None:
            return None
        policy = grant.policy
    if connection is None:
        connection = Connection.objects.filter(id=job.request.get("connection_id")).first()
    allocation = (job.manifest or {}).get("maximum_allocation")
    within = None if not allocation else not allocation_violations(
        job.request["resources"], allocation, policy["per_job_limits"])
    credential = credential or (job.credential if job.credential_id else None)
    return approval_requirements(policy, credential.execution_mode if credential else "inherit",
        connection.configuration if connection else {}, within_envelope=within)


def resource_validation(job, *, policy=None, connection=None):
    if policy is None:
        grant = Grant.objects.filter(project_id=job.project_id).first()
        if grant is None:
            return {"valid": None, "violations": [], "effective_limits": None,
                    "project_envelope_human_override_allowed": False}
        policy = grant.policy
    if connection is None:
        connection = Connection.objects.filter(id=job.request.get("connection_id")).first()
    effective = effective_resource_limits(policy, connection.configuration, backend=connection.backend) if connection else None
    allocation = (job.manifest or {}).get("maximum_allocation")
    if not allocation:
        return {"valid": None, "violations": [], "effective_limits": effective,
                "project_envelope_human_override_allowed": policy.get("mode") == "mixed"}
    violations = allocation_violations(job.request["resources"], allocation, policy["per_job_limits"])
    if connection:
        violations += violations_for_connection(job.request["resources"], allocation, connection.configuration)
    return {"valid": not violations, "violations": violations, "effective_limits": effective,
            "project_envelope_human_override_allowed": policy.get("mode") == "mixed"
                and not any(row["source"] == "cluster" for row in violations)}


def resource_error(violations):
    from .errors import BrokerError
    message = "Resource limits exceeded: " + "; ".join(
        f"{RESOURCE_LABELS[row['resource']]} {row['effective']} {row['unit']} exceeds the {row['source']} limit of {row['limit']} {row['unit']}"
        for row in violations) + ". Revise the request before retrying."
    return BrokerError("RESOURCE_LIMIT", message, 409, {"violations": violations})
