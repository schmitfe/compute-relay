"""Configured CPU allocation units and advisory Slurm topology evidence."""

from math import lcm


def allocated_cpus(requested, profile):
    """Round the configured minimum/request up to complete allocation units."""
    floor = profile.get("allocated_cpus", 1)
    unit = profile.get("cpu_allocation_quantum", 1)
    if any(type(value) is not int or not 1 <= value <= 1024 for value in (requested, floor, unit)):
        raise ValueError("CPU requests, allocation floors and units must be integers between 1 and 1024.")
    return ((max(requested, floor) + unit - 1) // unit) * unit


def _integer(value):
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 4:
        value = int(value)
    return value if type(value) is int and 1 <= value <= 1024 else None


def cpu_allocation_candidate(snapshot, selected_partitions=None):
    """Suggest an LCM unit from complete saved facts; never change a profile.

    A unit inferred from visible scheduler configuration is provisional: job
    submit plugins, topology changes and account policy can change allocation.
    Every selected partition/node must have supported facts before suggesting.
    """
    settings = snapshot.get("scheduler_settings", {})
    settings = settings if isinstance(settings, dict) else {}
    partitions = snapshot.get("partitions", [])
    partitions = partitions if isinstance(partitions, list) else []
    facts = {row["PartitionName"]: row for row in partitions
        if isinstance(row, dict) and isinstance(row.get("PartitionName"), str)}
    nodes = {}
    rows = snapshot.get("nodes", [])
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("partition"), str):
            name = row["partition"].removesuffix("*")
            nodes.setdefault(name, []).append(row)
    selected = sorted(set(selected_partitions if selected_partitions is not None else set(facts) | set(nodes)))
    evidence = []
    for name in selected:
        partition = facts.get(name, {})
        unit, reason = None, "Scheduler allocation settings or node topology are missing or unsupported."
        select = str(settings.get("SelectType", "")).lower()
        parameters = str(partition.get("SelectTypeParameters", "NONE")).upper()
        if parameters in {"", "NONE", "(NULL)"}:
            parameters = str(settings.get("SelectTypeParameters", "")).upper()
        flags = {flag.strip() for flag in parameters.split(",")}
        granularities = flags & {"CR_CPU", "CR_CPU_MEMORY", "CR_CORE", "CR_CORE_MEMORY", "CR_SOCKET", "CR_SOCKET_MEMORY"}
        if str(partition.get("OverSubscribe", "")).upper().startswith("EXCLUSIVE"):
            reason = "Exclusive whole-node allocation requires a separate verified allocation model."
        elif (name in facts and nodes.get(name) and select in {"select/cons_tres", "select/cons_res"}
                and len(granularities) == 1):
            mode = next(iter(granularities))
            if mode in {"CR_CPU", "CR_CPU_MEMORY"}:
                unit, reason = 1, "Slurm consumes individual configured logical CPUs."
            else:
                units = []
                for node in nodes.get(name, []):
                    cpus, sockets, cores, threads = [_integer(node.get(field))
                        for field in ("cpus", "sockets", "cores_per_socket", "threads_per_core")]
                    if None in (cpus, sockets, cores, threads) or sockets * cores * threads != cpus:
                        units = []
                        break
                    units.append(threads if mode in {"CR_CORE", "CR_CORE_MEMORY"} else cores * threads)
                if units:
                    unit = lcm(*units)
                    reason = "Whole-core allocation includes configured hardware threads." if "CORE" in mode else "Slurm consumes complete configured sockets."
        if unit is not None and not 1 <= unit <= 1024:
            unit, reason = None, "A conservative combined allocation unit exceeds the supported job size."
        evidence.append({"name": name, "unit": unit, "reason": reason})
    combined = lcm(*(row["unit"] for row in evidence)) if evidence and all(row["unit"] is not None for row in evidence) else None
    if combined is not None and combined > 1024:
        combined = None
    return {"status": "known" if combined else "unknown", "value": combined,
        "basis": "saved_scheduler_topology", "partitions": evidence,
        "note": "Provisional discovery suggestion. Confirm with the cluster owner and observed allocations before relying on it; saving requires the worker's normal configuration acknowledgement."}
