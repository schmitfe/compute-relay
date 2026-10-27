"""Conservative aggregation: visible idle hardware is not a placement guarantee."""

from datetime import datetime, timezone
from decimal import Decimal
import json
import re


def gpu_counts(value):
    counts = {}
    for match in re.finditer(r"(?:^|,)gpu:(?:([A-Za-z0-9_.-]+):)?([0-9]+)(?:\([^)]*\))?(?=,|$)", value):
        if len(match.group(2)) > 7:
            continue
        kind, amount = match.group(1) or "unspecified", int(match.group(2))
        counts[kind] = counts.get(kind, 0) + amount
    return counts


def parse_availability(response):
    partitions, seen = {}, set()
    def lines(name, maximum):
        query = response.get(name, {})
        return query.get("stdout", "").splitlines()[:maximum] if query.get("returncode") == 0 else []

    def partition(name):
        return partitions.setdefault(name, {"name": name, "nodes": 0, "schedulable_nodes": 0,
            "cpus": {"total": 0, "allocated": 0, "idle": 0, "allocation_known": True}, "gpus": {},
            "queue": {"running_jobs": 0, "pending_jobs": 0, "pending_by_gpu_type": {}, "pending_reasons": {}}})
    nodes, queue = response.get("nodes", {}), response.get("queue", {})
    cpu_states = {}
    for line in lines("cpus", 4096):
        fields = [value.strip() for value in line.split("|")]
        if len(fields) == 3:
            values = fields[2].split("/")
            if len(values) == 4 and all(value.isdigit() and len(value) <= 7 for value in values):
                allocated, idle, other, total = map(int, values)
                if allocated + idle + other == total:
                    cpu_states[(fields[1].removesuffix("*"), fields[0])] = (allocated, idle, total)
    for line in lines("nodes", 4096):
        fields = [value.strip() for value in line.split("|")]
        if len(fields) != 6:
            continue
        host, name, state, cpus, configured, used = fields
        name = name.removesuffix("*")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) or (name, host) in seen:
            continue
        seen.add((name, host))
        row = partition(name)
        row["nodes"] += 1
        ready = state.upper() in {"IDLE", "MIXED", "ALLOCATED"}
        row["schedulable_nodes"] += int(ready)
        total = int(cpus) if cpus.isdigit() and len(cpus) <= 7 else None
        row["cpus"]["total"] += total or 0
        counts = cpu_states.get((name, host))
        if counts and counts[2] == total:
            row["cpus"]["allocated"] += counts[0]
            row["cpus"]["idle"] += counts[1] if ready else 0
        else:
            row["cpus"]["allocation_known"] = False
        available = gpu_counts(configured)
        allocated = gpu_counts(used)
        for kind, total in available.items():
            target = row["gpus"].setdefault(kind, {"configured": 0, "schedulable": 0, "allocated": 0, "idle": 0, "allocation_known": True})
            target["configured"] += total
            target["schedulable"] += total if ready else 0
            # Missing or untyped usage cannot establish idle typed devices.
            # Shared GPU resources (MPS/shards) may also occupy whole devices.
            known = kind in allocated and allocated[kind] <= total and not re.search(r"(?:^|,)(?:mps|shard):", configured)
            target["allocation_known"] &= known
            if known:
                target["allocated"] += allocated.get(kind, 0)
                target["idle"] += max(0, total - allocated.get(kind, 0)) if ready else 0
    for line in lines("queue", 8192):
        fields = [value.strip() for value in line.split("|")]
        if len(fields) != 7:
            continue
        names, state, *requests, reason = fields
        for name in names.split(",")[:16]:
            name = name.removesuffix("*")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
                continue
            row = partition(name)["queue"]
            if state in {"RUNNING", "COMPLETING"}:
                row["running_jobs"] += 1
            elif state == "PENDING":
                row["pending_jobs"] += 1
                kinds = {kind for gres in requests for kind in gpu_counts(gres.replace("gres/", ""))}
                for kind in kinds:
                    row["pending_by_gpu_type"][kind] = row["pending_by_gpu_type"].get(kind, 0) + 1
                reason = reason.strip("()")
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", reason):
                    row["pending_reasons"][reason] = row["pending_reasons"].get(reason, 0) + 1
    pressure_known = nodes.get("returncode") == queue.get("returncode") == 0 and not any(
        value.get("truncated", False) for value in (nodes, queue)) and len(nodes.get("stdout", "").splitlines()) <= 4096 and len(queue.get("stdout", "").splitlines()) <= 8192
    for row in partitions.values():
        if not row["cpus"]["allocation_known"]:
            row["cpus"]["allocated"] = row["cpus"]["idle"] = None
        for gpu in row["gpus"].values():
            if not gpu["allocation_known"]:
                gpu["allocated"] = gpu["idle"] = None
        def ratio(jobs, capacity):
            # Control JSON deliberately permits only exact integer numbers.
            return format(Decimal(jobs) / Decimal(capacity), ".6f").rstrip("0").rstrip(".") if pressure_known and capacity else None
        pending_types = row["queue"]["pending_by_gpu_type"]
        row["queue_pressure"] = {"status": "approximate" if pressure_known and row["schedulable_nodes"] else "unknown",
            "ratio_encoding": "decimal_string",
            "pending_jobs_per_schedulable_node": ratio(row["queue"]["pending_jobs"], row["schedulable_nodes"]),
            "pending_gpu_jobs_per_schedulable_device": {kind: ratio(pending_types.get(kind, 0), row["gpus"].get(kind, {}).get("schedulable", 0))
                for kind in sorted(set(row["gpus"]) | set(pending_types))},
            "untyped_pending_gpu_jobs": pending_types.get("unspecified", 0)}
    result = {"observed_at": datetime.now(timezone.utc).isoformat(), "scope": "scheduler_visible",
        "nodes_status": "available" if nodes.get("returncode") == 0 else "unknown",
        "queue_status": "available" if queue.get("returncode") == 0 else "unknown",
        "cpu_allocation_status": "available" if response.get("cpus", {}).get("returncode") == 0 else "unknown",
        "truncated": any(value.get("truncated", False) for value in (nodes, queue, response.get("cpus", {})))
            or len(nodes.get("stdout", "").splitlines()) > 4096 or len(queue.get("stdout", "").splitlines()) > 8192
            or len(response.get("cpus", {}).get("stdout", "").splitlines()) > 4096 or len(partitions) > 128,
        "partitions": sorted(partitions.values(), key=lambda row: row["name"])[:128],
        "placement_guaranteed": False, "queue_wait_seconds": None,
        "limitations": ["Snapshot of scheduler-visible resources; no reservation or guaranteed start time.",
            "Queue visibility may be restricted by the site; pending array entries may be grouped.",
            "Partition totals may overlap; idle resources may be fragmented across nodes or constrained by account/QOS/reservations.",
            "Pressure ratios normalize visible job counts by schedulable capacity, not requested job size or duration; they are not wait-time estimates.",
            "Typed GPU ratios exclude untyped requests; compare untyped_pending_gpu_jobs as well. Site visibility and grouped arrays can undercount queue pressure.",
            "Admission/job limits are independent of current idle hardware; costs are not configured."]}
    # The internal report has a separate metadata ceiling. Retain only whole
    # partition observations and expose truncation instead of retrying forever.
    while len(json.dumps(result)) > 63000 and result["partitions"]:
        result["partitions"].pop()
        result["truncated"] = True
    return result
