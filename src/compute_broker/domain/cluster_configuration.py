"""Resource suggestions from saved Slurm facts, with no I/O or authority grants.

Visible maxima are independent single-node ceilings, not a schedulable resource
combination, allocation-rounding proof, or permission to use an account/QoS.
This module must remain usable by a worker without Django/controller imports.
"""

from copy import deepcopy
import re


RESOURCE_BOUNDS = {
    "maximum_cpus": (1, 1024), "maximum_gpus": (0, 16),
    "maximum_memory_mib": (128, 16777216), "maximum_time_limit_seconds": (60, 604800),
}
ALLOWLIST_FIELDS = ("partitions", "accounts", "qos")
TIMING_FIELDS = ("termination_margin_seconds", "overtime_margin_seconds", "cleanup_margin_seconds")
SAFETY_FIELDS = (*TIMING_FIELDS, "allocated_cpus", "allocated_gpus", "automatic_execution_allowed")
_FIELDS = set(RESOURCE_BOUNDS) | set(ALLOWLIST_FIELDS) | set(SAFETY_FIELDS)
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z")
_UNLIMITED = {"UNLIMITED", "INFINITE"}
_UNKNOWN_REASONS = {
    "maximum_cpus": "No supported finite single-node CPU ceiling could be established.",
    "maximum_gpus": "No unambiguous supported GPU count could be established.",
    "maximum_memory_mib": "No supported finite single-node memory ceiling could be established.",
    "maximum_time_limit_seconds": "No supported finite partition walltime was reported; missing does not mean unlimited.",
    "accounts": "No usable own-account associations were available; enter a site-authorized account explicitly.",
    "qos": "No usable own job-QoS associations were available; partition QoS is not a job-QoS grant.",
}


class ClusterConfigurationError(ValueError):
    pass


def _name(value):
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def _number(value):
    if type(value) is int:
        return value if 0 <= value <= 2**53 - 1 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value):
        result = int(value)
        return result if result <= 2**53 - 1 else None
    return None


def _walltime(value):
    """Slurm duration syntax; the caller rounds down to supported whole minutes."""
    if not isinstance(value, str) or len(value) > 80:
        return None
    if value.upper() in _UNLIMITED:
        return RESOURCE_BOUNDS["maximum_time_limit_seconds"][1]
    days, separator, clock = value.partition("-")
    if not separator:
        clock, days = days, None
    if not re.fullmatch(r"[0-9]{1,8}(?::[0-9]{1,2}){0,2}", clock):
        return None
    if days is not None and not re.fullmatch(r"[0-9]{1,8}", days):
        return None
    parts = [int(part) for part in clock.split(":")]
    if any(part >= 60 for part in parts[1:]):
        return None
    if days is not None:
        if parts[0] >= 24:
            return None
        parts += [0] * (3 - len(parts))
        return int(days) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 1:
        return parts[0] * 60
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _gpus(value):
    """One selectable GPU family per job; never add shared MPS/shard resources."""
    if not isinstance(value, str) or len(value) > 4096:
        return None, {}, "GPU advertisement is missing or malformed."
    if value in {"", "(null)", "N/A", "NONE"}:
        return 0, {}, None
    counts = {}
    untyped = None
    for token in re.sub(r"\([^()]*\)", "", value).split(","):
        if not token.startswith("gpu:"):
            if token == "gpu":
                return None, {}, "A GPU advertisement has no finite count."
            continue
        fields = token.split(":")
        if len(fields) == 2:
            count = _number(fields[1])
            if count is None or untyped is not None:
                return None, {}, "GPU counts are ambiguous."
            untyped = count
        elif len(fields) == 3 and _name(fields[1]):
            count = _number(fields[2])
            if count is None or fields[1] in counts:
                return None, {}, "GPU counts are ambiguous."
            counts[fields[1]] = count
        else:
            return None, {}, "A GPU advertisement is unsupported."
    if untyped is not None and counts and untyped != sum(counts.values()):
        return None, {}, "Generic and typed GPU counts disagree."
    if counts:
        return max(counts.values()), counts, None
    if untyped is not None:
        return untyped, {}, "GPU count is advertised without a selectable type."
    return 0, {}, None


def _supported(field, value, warnings):
    if value is None:
        return None
    minimum, maximum = RESOURCE_BOUNDS[field]
    if field == "maximum_time_limit_seconds" and value % 60:
        warnings.add("Walltime is rounded down to whole minutes.")
        value -= value % 60
    if value < minimum:
        warnings.add(field + " is below the supported minimum; enter a verified value or exclude that partition.")
        return None
    if value > maximum:
        warnings.add(field + " is capped at the version-one implementation limit " + str(maximum) + ".")
        value = maximum
    return value


def _partition_evidence(name, facts, nodes):
    warnings = set()
    def unknown_limit(field):
        value = facts.get(field)
        unlimited = isinstance(value, str) and (value.upper() in _UNLIMITED or value == "0")
        return value is not None and not unlimited and _number(value) is None
    unknown_cpu_cap = unknown_limit("MaxCPUsPerNode")
    unknown_memory_cap = unknown_limit("MaxMemPerNode") or unknown_limit("MaxMemPerCPU")
    if unknown_cpu_cap or unknown_memory_cap:
        warnings.add("A partition CPU/memory limit is unsupported or malformed; its constrained resource ceiling remains unknown.")
    cpu_limit = _number(facts.get("MaxCPUsPerNode")) or None
    memory_limit = _number(facts.get("MaxMemPerNode")) or None
    per_cpu_memory = _number(facts.get("MaxMemPerCPU")) or None
    limits = {field: [] for field in RESOURCE_BOUNDS}
    gpu_types = {}
    for node in nodes:
        cpus, memory = _number(node.get("cpus")), _number(node.get("memory_mib"))
        if unknown_cpu_cap:
            cpus = None
        if unknown_memory_cap or (per_cpu_memory is not None and cpus is None):
            memory = None
        if cpus is None or memory is None:
            warnings.add("Some node CPU or memory facts are missing; known maxima may describe only part of this partition.")
        if cpus is not None and cpu_limit is not None:
            cpus = min(cpus, cpu_limit)
        if memory is not None and memory_limit is not None:
            memory = min(memory, memory_limit)
        if memory is not None and per_cpu_memory is not None and cpus is not None:
            memory = min(memory, per_cpu_memory * cpus)
        gpu_count, types, warning = _gpus(node.get("gres"))
        if warning:
            warnings.add(warning)
        for gpu_type, count in types.items():
            gpu_types[gpu_type] = max(gpu_types.get(gpu_type, 0), count)
        for field, value in (("maximum_cpus", cpus), ("maximum_memory_mib", memory), ("maximum_gpus", gpu_count)):
            if value is not None:
                limits[field].append(value)
    walltime = _walltime(facts.get("MaxTime"))
    if isinstance(facts.get("MaxTime"), str) and facts["MaxTime"].upper() in _UNLIMITED:
        warnings.add("Unlimited scheduler walltime is capped at the version-one limit of 604800 seconds.")
    values = {field: _supported(field, walltime if field == "maximum_time_limit_seconds"
        else max(limits[field], default=None), warnings) for field in RESOURCE_BOUNDS}
    return {"name": name, **values, "gpu_types": dict(sorted(gpu_types.items())),
        "unknowns": [field for field, value in values.items() if value is None], "warnings": sorted(warnings)}


def derive_cluster_configuration(snapshot, current_profile=None):
    """Fill missing settings, preserving owner edits and existing safety evidence.

    Return site_profile plus discovered_limits, per-partition evidence, unknowns
    and warnings. Missing facts remain absent from site_profile. No value from
    timing_candidates, scheduler_settings, partition totals or live state grants
    authority or verifies allocation/cleanup behavior.
    """
    if not isinstance(snapshot, dict) or (current_profile is not None and not isinstance(current_profile, dict)):
        raise ClusterConfigurationError("Expected a discovery snapshot and configuration objects.")
    current = validate_cluster_configuration(current_profile or {}, require_limits=False)
    facts = {}
    for partition in snapshot.get("partitions", []) if isinstance(snapshot.get("partitions", []), list) else []:
        name = partition.get("PartitionName") if isinstance(partition, dict) else partition
        if _name(name):
            facts.setdefault(name, partition if isinstance(partition, dict) else {})
    nodes = {}
    for node in snapshot.get("nodes", []) if isinstance(snapshot.get("nodes", []), list) else []:
        if not isinstance(node, dict):
            continue
        name = node.get("partition")
        if isinstance(name, str):
            name = name.removesuffix("*")
        if _name(name):
            facts.setdefault(name, {})
            nodes.setdefault(name, []).append(node)
    partitions = [_partition_evidence(name, facts[name], nodes.get(name, [])) for name in sorted(facts)]
    discovered = {field: max((item[field] for item in partitions if item[field] is not None), default=None)
        for field in RESOURCE_BOUNDS}
    accounts, qos = set(), set()
    if snapshot.get("association_query_status") == "available":
        for row in snapshot.get("associations", []) if isinstance(snapshot.get("associations", []), list) else []:
            if not isinstance(row, dict):
                continue
            if _name(row.get("account")):
                accounts.add(row["account"])
            for field in ("qos", "default_qos"):
                if isinstance(row.get(field), str):
                    qos.update(value for value in row[field].split(",") if _name(value) and value not in {"ALL", "NONE"})
    suggested = {"partitions": sorted(facts), "accounts": sorted(accounts), "qos": sorted(qos),
        **{key: value for key, value in discovered.items() if value is not None}}
    profile = {**suggested, **current}
    profile["automatic_execution_allowed"] = current.get("automatic_execution_allowed") is True
    profile = validate_cluster_configuration(profile, require_limits=False)
    unknowns = [field for field, value in discovered.items() if value is None]
    if not accounts:
        unknowns.append("accounts")
    if not qos:
        unknowns.append("qos")
    warnings = {"Visible resources and association snapshots do not grant scheduler or project authorization.",
        "Resource maxima are independent single-node ceilings; they may come from different nodes or partitions.",
        "Allocation rounding, account/QoS limits and cleanup bounds still require verification."}
    warnings.update(warning for partition in partitions for warning in partition["warnings"])
    if any(item["unknowns"] for item in partitions):
        warnings.add("Some partition limits are unknown; aggregate known maxima do not apply to every partition.")
    return {"site_profile": profile, "discovered_limits": discovered, "partitions": partitions,
        "unknowns": unknowns, "unknown_reasons": {field: _UNKNOWN_REASONS[field] for field in unknowns},
        "warnings": sorted(warnings)}


def validate_cluster_configuration(profile, *, require_limits=True):
    """Validate a finite version-one profile, without asserting site verification."""
    if not isinstance(profile, dict) or set(profile) - _FIELDS:
        raise ClusterConfigurationError("Cluster configuration has unsupported fields.")
    result = deepcopy(profile)
    for field in ALLOWLIST_FIELDS:
        if field in result and (not isinstance(result[field], list) or len(result[field]) > 1000
                or any(not _name(value) for value in result[field]) or len(set(result[field])) != len(result[field])):
            raise ClusterConfigurationError("Cluster scheduler names must be unique safe names.")
    if require_limits and not result.get("partitions"):
        raise ClusterConfigurationError("Select at least one partition.")
    for field, (minimum, maximum) in RESOURCE_BOUNDS.items():
        if field not in result and not require_limits:
            continue
        if type(result.get(field)) is not int or not minimum <= result[field] <= maximum:
            raise ClusterConfigurationError("Enter a supported finite value for " + field + ".")
    if "maximum_time_limit_seconds" in result and result["maximum_time_limit_seconds"] % 60:
        raise ClusterConfigurationError("Walltime must be a whole number of minutes.")
    for field in SAFETY_FIELDS:
        if field not in result:
            continue
        value = result[field]
        if field == "automatic_execution_allowed":
            if type(value) is not bool:
                raise ClusterConfigurationError("Automatic execution requires an explicit boolean setting.")
        else:
            minimum, maximum = ((1, 1024) if field == "allocated_cpus" else
                (0, 16) if field == "allocated_gpus" else (0, 604800))
            if type(value) is not int or not minimum <= value <= maximum:
                raise ClusterConfigurationError("A retained allocation or timing setting is invalid.")
    for field, floor in (("maximum_cpus", "allocated_cpus"), ("maximum_gpus", "allocated_gpus")):
        if field in result and floor in result and result[field] < result[floor]:
            raise ClusterConfigurationError("Resource ceilings cannot be lower than retained allocation rounding.")
    if ("maximum_time_limit_seconds" in result and all(field in result for field in SAFETY_FIELDS[:3])
            and result["maximum_time_limit_seconds"] + sum(result[field] for field in SAFETY_FIELDS[:3]) > 1209600):
        raise ClusterConfigurationError("Walltime and retained timing margins exceed the supported charge duration.")
    return result
