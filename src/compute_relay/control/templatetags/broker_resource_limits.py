"""Human labels for authoritative resource summaries; no admission decisions."""

from django import template


register = template.Library()
_RESOURCES = {
    "nodes": ("Nodes", "nodes", None),
    "cpu_cores": ("CPUs", "CPU cores", "maximum_cpus"),
    "gpu_devices": ("GPUs", "GPU devices", "maximum_gpus"),
    "memory_mib": ("Memory", "MiB", "maximum_memory_mib"),
    "time_limit_seconds": ("Walltime", "seconds", "maximum_time_limit_seconds"),
}


@register.filter
def resource_label(resource):
    return _RESOURCES.get(resource, ("Resource",))[0] if isinstance(resource, str) else "Resource"


@register.filter
def resource_source(source):
    return {"project": "Project", "cluster": "Cluster", "implementation": "Broker"}.get(source, "Configured limit") if isinstance(source, str) else "Configured limit"


@register.filter
def effective_resource_rows(limits):
    """Display only known public numeric fields, never arbitrary configuration."""
    if not isinstance(limits, dict):
        return []
    active, project, cluster = (limits.get(field, {}) for field in ("per_job_limits", "project_limits", "cluster_limits"))
    if not all(isinstance(value, dict) for value in (active, project, cluster)):
        return []

    def number(value):
        return value if type(value) is int and 0 <= value <= 2**53 - 1 else None

    return [{"resource": resource, "label": label, "unit": unit, "effective": number(active.get(resource)),
             "project": number(project.get(resource)), "cluster": number(cluster.get(cluster_field))}
            for resource, (label, unit, cluster_field) in _RESOURCES.items() if resource != "nodes"]
