"""Convenience requests still pass through normal immutable jobs and admission."""

from copy import deepcopy

from compute_relay.control.errors import BrokerError

from . import services
from .models import EnvironmentBuild


def defaults(connection, resources=None, slurm=None, policy=None, project_policy=None):
    limits = policy.get("resource_limits", {}) if policy else {}
    project_limits = (project_policy or {}).get("per_job_limits", {})
    site = connection.configuration.get("site_profile", {})
    selected = resources if resources is not None else {"nodes": 1, "cpus": 1, "gpus": 0,
        "memory_mib": min(512, limits.get("memory_mib", 512), project_limits.get("memory_mib", 512), site.get("maximum_memory_mib", 512)),
        "time_limit_seconds": min(300, limits.get("time_limit_seconds", 60), project_limits.get("time_limit_seconds", 300), site.get("maximum_time_limit_seconds", 300))}
    if slurm is not None and not isinstance(slurm, dict):
        raise ValueError("Invalid scheduler options")
    options = {key: site[field][0] for key, field in (("partition", "partitions"), ("account", "accounts"), ("qos", "qos")) if len(site.get(field, [])) == 1}
    options.update(slurm or {})
    return selected, options


def require_scheduler_options(connection, slurm):
    if connection.backend != "slurm":
        return
    site = connection.configuration.get("site_profile", {})
    for key, field in (("partition", "partitions"), ("account", "accounts"), ("qos", "qos")):
        choices = site.get(field, [])
        if (key == "partition" or choices) and not slurm.get(key):
            raise BrokerError("SCHEDULER_OPTIONS_REQUIRED", "Select scheduler options from list_compute_targets and retry the environment tool.", 400)
        if key in slurm and choices and slurm[key] not in choices:
            raise BrokerError("SCHEDULER_OPTIONS_DENIED", "Select permitted scheduler options from list_compute_targets.", 400)


def package_recipe(credential, environment, data):
    policy = services._policy(credential.project)
    from compute_relay.control.models import Grant
    job_policy = Grant.objects.get(project=credential.project).policy
    packages = data["packages"]
    if not isinstance(packages, list) or not 1 <= len(packages) <= 64:
        raise ValueError("Supply one through 64 hash-pinned wheel descriptors")
    parent = EnvironmentBuild.objects.filter(environment=environment, spec__project=credential.project, state="ready").select_related("spec").first()
    if parent:
        document = deepcopy(parent.spec.document)
        document["parent_environment"] = {"id": str(environment.id), "revision_sha256": environment.revision_sha256}
        for field in ("platform_tag", "python_major_minor", "connection_revision_sha256"):
            document.pop(field, None)
    else:
        services._bootstrap(environment)
        resources, slurm = defaults(environment.connection, data.get("resources"), data.get("slurm"), policy.configuration, job_policy)
        document = {"connection_id": str(environment.connection_id), "base_environment": {
            "id": str(environment.id), "revision_sha256": environment.revision_sha256}, "manager": "venv", "requirements": [],
            "resources": resources, "slurm": slurm,
            "estimated_bytes": min(512 * 1024**2, policy.configuration["max_environment_bytes"]),
            "estimated_files": min(5000, policy.configuration["max_environment_files"])}
    import re
    def name(row):
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValueError("Invalid requested package")
        return re.sub(r"[-_.]+", "-", row["name"]).lower()
    merged = {name(row): row for row in document["requirements"]}
    if len({name(row) for row in packages}) != len(packages):
        raise ValueError("Repeated package name")
    merged.update({name(row): row for row in packages})
    document["requirements"] = list(merged.values())
    document["name"] = data.get("name") or environment.name[:60] + "-packages"
    for field in ("resources", "slurm"):
        if data.get(field) is not None:
            document[field] = {**document[field], **data[field]} if field == "slurm" and isinstance(data[field], dict) else data[field]
    document["resources"], document["slurm"] = defaults(environment.connection, document["resources"], document["slurm"])
    require_scheduler_options(environment.connection, document["slurm"])
    return document
