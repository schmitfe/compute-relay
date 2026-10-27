"""Convenience requests still pass through normal immutable jobs and admission."""

from copy import deepcopy

from . import services
from .models import EnvironmentBuild


def defaults(connection, resources=None, slurm=None, policy=None, project_policy=None):
    limits = policy.get("resource_limits", {}) if policy else {}
    project_limits = (project_policy or {}).get("per_job_limits", {})
    site = connection.configuration.get("site_profile", {})
    selected = resources if resources is not None else {"nodes": 1, "cpus": 1, "gpus": 0,
        "memory_mib": min(512, limits.get("memory_mib", 512), project_limits.get("memory_mib", 512), site.get("maximum_memory_mib", 512)),
        "time_limit_seconds": min(300, limits.get("time_limit_seconds", 60), project_limits.get("time_limit_seconds", 300), site.get("maximum_time_limit_seconds", 300))}
    if slurm is None:
        profile = connection.configuration.get("site_profile", {})
        slurm = {key: profile[field][0] for key, field in (("partition", "partitions"), ("account", "accounts")) if len(profile.get(field, [])) == 1}
    return selected, slurm


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
            document[field] = data[field]
    return document
