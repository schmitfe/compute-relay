"""Bounded presentation of saved partition/node facts; no scheduler queries.

Slurm permits compressed node lists and overlapping partitions. Count unique
saved names, never table rows, and keep unsupported/inconsistent facts unknown.
Node range syntax: https://slurm.schedmd.com/sinfo.html
"""

from __future__ import annotations

import re


MAX_EXPANDED_NODES = 100000
MAX_PARTITIONS = 512
MAX_NODE_ROWS = 20000
MAX_HOSTLIST_LENGTH = 16384
MAX_MEMBERSHIP_LINKS = 200000
MAX_DETAIL_ROWS = 30000
MAX_NODE_VISITS = 200000

_FIELDS = (("TotalCPUs", "Total CPUs"), ("MaxTime", "Maximum job time"), ("DefaultTime", "Default job time"),
           ("MinNodes", "Minimum nodes per job"), ("MaxNodes", "Maximum nodes per job"),
           ("DefMemPerCPU", "Default memory per CPU"), ("DefMemPerNode", "Default memory per node"),
           ("MaxMemPerCPU", "Maximum memory per CPU"), ("MaxMemPerNode", "Maximum memory per node"))


def _hostlist(value, limit):
    if (not isinstance(value, str) or not value or len(value) > MAX_HOSTLIST_LENGTH
            or value.upper() in {"NONE", "ALL", "N/A", "(NULL)"} or limit <= 0):
        return None
    tokens, start, depth = [], 0, 0
    for index, character in enumerate(value):
        if character == "[":
            depth += 1
            if depth != 1:
                return None
        elif character == "]":
            depth -= 1
            if depth != 0:
                return None
        elif character == "," and depth == 0:
            tokens.append(value[start:index])
            start = index + 1
    if depth:
        return None
    tokens.append(value[start:])
    names = set()
    for token in tokens:
        if not token:
            return None
        expanded = [""]
        for part in re.split(r"(\[[^\[\]]*\])", token):
            if not part:
                continue
            options = []
            if part.startswith("["):
                for item in part[1:-1].split(","):
                    match = re.fullmatch(r"([0-9]{1,10})(?:-([0-9]{1,10}))?", item)
                    if not match:
                        return None
                    low, high = match.groups()
                    if high is None:
                        options.append(low)
                    else:
                        first, last = int(low), int(high)
                        if first > last or last - first + 1 > limit:
                            return None
                        padded = (low.startswith("0") and len(low) > 1) or (high.startswith("0") and len(high) > 1)
                        if padded and len(low) != len(high):
                            return None
                        options.extend(str(number).zfill(len(low)) if padded else str(number) for number in range(first, last + 1))
                    if len(options) > limit:
                        return None
            elif re.fullmatch(r"[A-Za-z0-9_.-]+", part):
                options = [part]
            else:
                return None
            if len(expanded) * len(options) > limit:
                return None
            expanded = [prefix + suffix for prefix in expanded for suffix in options]
        names.update(expanded)
        if len(names) > limit:
            return None
    return names


def _partition_name(value):
    return value.strip().removesuffix("*") if isinstance(value, str) and 0 < len(value.strip()) <= 255 else None


def _reported_count(value):
    if type(value) is int:
        return value if 0 <= value <= 2**53 - 1 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value):
        return _reported_count(int(value))
    return None


def partition_resources(snapshot):
    """Return display groups for the cluster detail template's context."""
    if not isinstance(snapshot, dict):
        return []
    partitions = snapshot.get("partitions", [])
    nodes = snapshot.get("nodes", [])
    partitions = partitions if isinstance(partitions, list) else []
    nodes = nodes if isinstance(nodes, list) else []
    groups, cache, remaining = {}, {}, MAX_EXPANDED_NODES
    limited = len(nodes) > MAX_NODE_ROWS or len(partitions) > MAX_PARTITIONS

    def hosts(value):
        nonlocal remaining
        if not isinstance(value, str):
            return None
        if value in memberships:
            return {value}
        if value not in cache:
            cache[value] = _hostlist(value, remaining)
            if cache[value] is not None:
                remaining -= len(cache[value])
        return cache[value]

    def group(name):
        nonlocal limited
        if name not in groups:
            if len(groups) >= MAX_PARTITIONS:
                limited = True
                return None
            groups[name] = {"name": name, "nodes": [], "seen_rows": set(), "observed": set(), "membership": None,
                            "membership_present": False, "invalid_nodes": False, "reported": None, "fields": []}
        return groups[name]

    memberships, link_count = {}, 0
    for partition in partitions[:MAX_PARTITIONS]:
        if not isinstance(partition, dict):
            continue
        name = _partition_name(partition.get("PartitionName", partition.get("name")))
        current = group(name) if name else None
        if current is None:
            continue
        current["reported"] = _reported_count(partition.get("TotalNodes"))
        current["fields"] = [{"label": label, "value": partition[key]} for key, label in _FIELDS
                             if isinstance(partition.get(key), (str, int)) and not isinstance(partition[key], bool)]
        current["membership_present"] = "Nodes" in partition
        current["membership"] = hosts(partition.get("Nodes"))
        for node_name in current["membership"] or ():
            if link_count >= MAX_MEMBERSHIP_LINKS:
                limited = True
                break
            memberships.setdefault(node_name, set()).add(name)
            link_count += 1

    detail_count, visits = 0, 0
    for node in nodes[:MAX_NODE_ROWS]:
        if detail_count >= MAX_DETAIL_ROWS:
            limited = True
            break
        if not isinstance(node, dict):
            continue
        names = hosts(node.get("name"))
        visits += len(names) if names is not None else 1
        if visits > MAX_NODE_VISITS:
            limited = True
            break
        direct = node.get("partition", "")
        direct = {_partition_name(name) for name in direct.split(",")} if isinstance(direct, str) else set()
        direct.discard(None)
        matched = {}
        for name in names or ():
            for partition in memberships.get(name, ()):
                matched.setdefault(partition, set()).add(name)
        targets = direct | set(matched)
        if not targets:
            targets = {"Unspecified partition"}
        for name in sorted(targets):
            current = group(name)
            if current is None:
                continue
            selected = names if name in direct or name == "Unspecified partition" else matched[name]
            if selected is None:
                current["invalid_nodes"] = True
            else:
                current["observed"].update(selected)
            display_name = node.get("name", "Unknown")
            if selected is not None and names is not None and selected != names:
                display_name = ", ".join(sorted(selected)[:100])
                if len(selected) > 100:
                    display_name += " …"
                    limited = True
            row = {key: value if isinstance(value, (str, int)) and not isinstance(value, bool) else "Unknown"
                   for key, value in (("name", display_name), ("cpus", node.get("cpus")),
                                      ("memory_mib", node.get("memory_mib")), ("gres", node.get("gres")))}
            signature = tuple(row.values())
            if signature not in current["seen_rows"]:
                if detail_count >= MAX_DETAIL_ROWS:
                    limited = True
                    break
                current["nodes"].append(row)
                current["seen_rows"].add(signature)
                detail_count += 1

    result = []
    for current in groups.values():
        membership, observed, reported = current["membership"], current["observed"], current["reported"]
        inconsistent = membership is not None and not observed <= membership
        if reported is not None:
            inconsistent |= len(observed) > reported or membership is not None and len(membership) != reported
            count = None if inconsistent else reported
        elif membership is not None:
            count = None if inconsistent else len(membership)
        else:
            unknown = limited or current["invalid_nodes"] or current["membership_present"]
            count = len(observed) if observed and not unknown else None
        result.append({"name": current["name"], "node_count": count, "nodes": current["nodes"], "fields": current["fields"],
                       "details_limited": limited, "inconsistent": inconsistent})
    return result
