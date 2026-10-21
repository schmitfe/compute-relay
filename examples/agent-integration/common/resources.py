"""Read effective Linux memory without allocating it or invoking commands."""

from pathlib import Path, PurePosixPath
import re


def _unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def effective_memory(proc=Path("/proc")):
    proc = Path(proc)
    try:
        entries = {line.split(":", 1)[0]: int(line.split()[1]) * 1024 for line in (proc / "meminfo").read_text().splitlines()
                   if line.startswith(("MemTotal:", "MemAvailable:"))}
        total, available = entries["MemTotal"], entries["MemAvailable"]
        groups = [line.split(":", 2) for line in (proc / "self/cgroup").read_text().splitlines()]
        mounts = (proc / "self/mountinfo").read_text().splitlines()
    except (OSError, ValueError, KeyError, IndexError):
        return {"supported": False, "reason": "Linux MemTotal/MemAvailable and cgroup metadata are required"}
    limits, headrooms, observations = [total], [available], []
    resolved = True
    memory_groups = [(controllers, path) for _, controllers, path in groups if controllers == "" or "memory" in controllers.split(",")]
    for controllers, membership in memory_groups:
        matched = False
        for mount in mounts:
            try:
                left, right = mount.split(" - ", 1)
                fields, tail = left.split(), right.split()
                if not ((controllers == "" and tail[0] == "cgroup2") or
                        ("memory" in controllers.split(",") and tail[0] == "cgroup" and "memory" in tail[2].split(","))):
                    continue
                root, point = PurePosixPath(_unescape(fields[3])), Path(_unescape(fields[4]))
                group = PurePosixPath(membership)
                if ".." in group.parts:
                    continue
                relative = PurePosixPath(".") if str(group) == "/" else group.relative_to(root)
                directory = point.joinpath(*relative.parts)
                matched = True
                for _ in range(128):
                    maximum = directory / ("memory.max" if controllers == "" else "memory.limit_in_bytes")
                    current = directory / ("memory.current" if controllers == "" else "memory.usage_in_bytes")
                    if maximum.exists():
                        text = maximum.read_text().strip()
                        if text != "max":
                            cap, used = int(text), int(current.read_text().strip())
                            if cap < 0 or used < 0:
                                raise ValueError("Negative cgroup memory accounting")
                            if cap < 2**60:  # v1 uses very large values to represent no limit.
                                limits.append(cap)
                                headrooms.append(max(0, cap - used))
                                observations.append({"version": 2 if controllers == "" else 1, "limit_bytes": cap, "used_bytes": used})
                    if directory == point:
                        break
                    if point not in directory.parents:
                        raise ValueError("Unresolved cgroup ancestor")
                    directory = directory.parent
                else:
                    raise ValueError("Too many cgroup ancestors")
            except (ValueError, OSError, IndexError):
                resolved = False
        if not matched:
            resolved = False
    return {"supported": True, "cgroup_resolution_complete": resolved,
            "host_total_bytes": total, "host_available_bytes": available,
            "effective_limit_bytes": min(limits), "effective_available_bytes": min(headrooms), "cgroups": observations}
