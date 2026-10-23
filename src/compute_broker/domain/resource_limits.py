"""Pure planning limits and project-envelope diagnostics.

The advertised values describe configured broker ceilings. They neither reserve
resources nor establish scheduler capacity, account/QoS permission, or whether
the independent CPU, GPU and memory maxima coexist on one node.
"""

from copy import deepcopy

from .cluster_configuration import RESOURCE_BOUNDS


_RESOURCE_FIELDS = {
    "cpu_cores": "maximum_cpus",
    "gpu_devices": "maximum_gpus",
    "memory_mib": "maximum_memory_mib",
    "time_limit_seconds": "maximum_time_limit_seconds",
}
_MINIMUM_REQUEST = {
    "nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60,
}
_SAFE_INTEGER = 2**53 - 1


def _integer(value, *, minimum=0, maximum=_SAFE_INTEGER):
    return value if type(value) is int and minimum <= value <= maximum else None


def effective_resource_limits(project_policy, connection_configuration, *, backend="slurm"):
    """Intersect configured project, cluster and version-one request ceilings.

    ``project_policy`` is the full policy (including ``mode``/``per_job_limits``)
    and ``connection_configuration`` contains ``site_profile``. Missing cluster
    maxima impose no additional configured cap, as in package preparation; they
    remain explicitly unknown as scheduler evidence. Invalid configured values
    never become permissive defaults. Legacy timing margins and charged-duration
    caps do not constrain new jobs; requested walltime is the reservation basis.

    ``feasible`` indicates whether a minimum request can fit these configured
    limits, not whether Slurm can run it. ``None`` means required information is
    unknown. Mixed-policy human overrides remain a separate authorization path.
    """
    if not isinstance(project_policy, dict) or not isinstance(connection_configuration, dict):
        raise ValueError("Expected project policy and connection configuration objects.")
    project_limits = project_policy.get("per_job_limits", {})
    profile = connection_configuration.get("site_profile", {})
    if not isinstance(project_limits, dict) or not isinstance(profile, dict):
        raise ValueError("Expected project per-job limits and cluster profile objects.")
    limits = {"nodes": 1}
    unknowns, reasons = [], []
    required_unknown = False
    cluster_limits = {key: deepcopy(profile.get(key)) for key in _RESOURCE_FIELDS.values()}
    for resource, field in _RESOURCE_FIELDS.items():
        project_value = _integer(project_limits.get(resource))
        cluster_value = _integer(profile.get(field))
        if project_value is None:
            unknowns.append("project_limits." + resource)
            required_unknown = True
        if cluster_value is None:
            unknowns.append("cluster_limits." + field)
            if field in profile:
                required_unknown = True
        if project_value is None or (field in profile and cluster_value is None):
            limits[resource] = None
        else:
            candidates = [project_value, RESOURCE_BOUNDS[field][1]]
            if cluster_value is not None:
                candidates.append(cluster_value)
            limits[resource] = min(candidates)

    floors = {}
    for resource, field, default, minimum, maximum in (
        ("cpu_cores", "allocated_cpus", 1, 1, 1024),
        ("gpu_devices", "allocated_gpus", 0, 0, 16),
    ):
        floor = _integer(profile.get(field, default), minimum=minimum, maximum=maximum)
        floors[resource] = floor
        if floor is None:
            unknowns.append("allocation_floors." + resource)
            required_unknown = True
        elif limits[resource] is not None and floor > limits[resource]:
            reasons.append(f"The cluster allocation floor for {resource} ({floor}) exceeds the effective ceiling ({limits[resource]}).")

    unit = _integer(profile.get("cpu_allocation_quantum", 1), minimum=1, maximum=1024)
    maximum_request_cpus = None
    if unit is None:
        unknowns.append("allocation_quantum.cpu_cores")
        required_unknown = True
    elif floors["cpu_cores"] is not None and limits["cpu_cores"] is not None:
        minimum_allocation = ((floors["cpu_cores"] + unit - 1) // unit) * unit
        maximum_request_cpus = (limits["cpu_cores"] // unit) * unit
        if minimum_allocation > limits["cpu_cores"] and floors["cpu_cores"] <= limits["cpu_cores"]:
            reasons.append("The smallest rounded CPU allocation exceeds the effective CPU ceiling.")

    if limits["time_limit_seconds"] is not None:
        limits["time_limit_seconds"] -= limits["time_limit_seconds"] % 60
    for resource, minimum in (("memory_mib", 128), ("time_limit_seconds", 60)):
        if limits[resource] is not None and limits[resource] < minimum:
            reasons.append(f"The effective {resource} ceiling ({limits[resource]}) is below the minimum supported request ({minimum}).")
    return {
        "per_job_limits": limits,
        "allocation_floors": floors,
        "allocation_quantum": {"cpu_cores": unit},
        "maximum_request_cpus": maximum_request_cpus,
        "allocation_prediction": {"basis": "configured_unit" if "cpu_allocation_quantum" in profile else "request_and_configured_floor_only",
            "scheduler_verified": False},
        "minimum_request": deepcopy(_MINIMUM_REQUEST),
        "feasible": False if reasons else None if required_unknown else True,
        "blocking_reasons": reasons,
        "unknowns": unknowns,
        "project_limits": {key: deepcopy(project_limits.get(key)) for key in _RESOURCE_FIELDS},
        "cluster_limits": cluster_limits,
        "project_envelope_human_override_allowed": project_policy.get("mode") == "mixed",
        "notes": [
            "These are per-job configured ceilings, not a reservation or a statement of live cluster capacity.",
            "Independent maxima may come from different nodes or partitions; account/QoS limits and current availability are not established here.",
            "Reservations use requested walltime; final settlement uses actual allocated elapsed time and can exceed the reservation.",
            "CPU allocations round up to complete configured units. Without an explicit unit, scheduler rounding is unverified; a minimum floor alone does not model 3 CPUs rounding to 4.",
            "Current project allowance, concurrent-job limits and admission checks still apply at execution.",
        ],
    }


def allocation_violations(request_resources, maximum_allocation, project_limits):
    """Report every prepared-allocation violation of a validated project envelope.

    CPU/GPU allocation rounding is taken from the prepared manifest. Version one
    has no memory-allocation rounding, so memory comes from the request. Missing
    request CPU/GPU/walltime values remain null in diagnostics (legacy internal
    fixtures may omit them); effective allocation and limit values must be valid.
    """
    if not all(isinstance(value, dict) for value in (request_resources, maximum_allocation, project_limits)):
        raise ValueError("Expected resources, maximum allocation and project limits objects.")
    violations = []
    for resource, request_field, unit in (
        ("cpu_cores", "cpus", "cores"),
        ("gpu_devices", "gpus", "devices"),
        ("memory_mib", "memory_mib", "MiB"),
        ("time_limit_seconds", "time_limit_seconds", "seconds"),
    ):
        requested = _integer(request_resources.get(request_field))
        effective = _integer((request_resources if resource == "memory_mib" else maximum_allocation).get(resource))
        limit = _integer(project_limits.get(resource))
        if effective is None or limit is None:
            raise ValueError("Expected a finite allocation and project limit for " + resource + ".")
        if effective > limit:
            violations.append({"resource": resource, "requested": requested, "effective": effective,
                "limit": limit, "excess": effective - limit, "source": "project", "unit": unit})
    return violations
