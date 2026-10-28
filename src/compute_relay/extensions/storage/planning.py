"""Pure optional-storage validation and planning; never probes or writes a disk."""

from __future__ import annotations

import posixpath
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

SAFE_INTEGER = 2**53 - 1
ROLES = ("work", "environments", "data", "results", "scratch")
_ALIAS = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z")


class StoragePlanningError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def integer(value, *, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not int or not 0 <= value <= SAFE_INTEGER:
        raise StoragePlanningError("INVALID_STORAGE", "Storage counters must be nonnegative safe integers.")
    return value


def total(values):
    result = sum(values)
    return integer(result)


def canonical_root(value):
    if (not isinstance(value, str) or len(value) > 4096 or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
            or value == "/" or value.startswith("//") or posixpath.normpath(value) != value
            or any(part in {".", ".."} for part in value.split("/"))):
        raise StoragePlanningError("INVALID_STORAGE_ROOT", "Choose a canonical dedicated storage root.")
    return value


def validate_storage_roots(value):
    """Sanitized, operator-provided profile metadata; no endpoints or commands."""
    if not isinstance(value, list) or len(value) > 32:
        raise StoragePlanningError("INVALID_STORAGE_ROOT", "At most 32 storage roots may be advertised.")
    result, aliases = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {"alias", "path", "roles", "capabilities", "filesystem_id"}:
            raise StoragePlanningError("INVALID_STORAGE_ROOT", "Unrecognized storage root metadata.")
        alias, roles = item.get("alias"), item.get("roles")
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias) or alias in aliases:
            raise StoragePlanningError("INVALID_STORAGE_ROOT", "Storage aliases must be distinct safe names.")
        aliases.add(alias)
        if not isinstance(roles, list) or not roles or any(role not in ROLES for role in roles) or len(set(roles)) != len(roles):
            raise StoragePlanningError("INVALID_STORAGE_ROOT", "Choose distinct supported storage roles.")
        capabilities = item.get("capabilities", {})
        if not isinstance(capabilities, dict) or set(capabilities) - {"visibility", "formats", "quota_reporting"}:
            raise StoragePlanningError("INVALID_STORAGE_ROOT", "Unrecognized storage capability.")
        visibility = capabilities.get("visibility", "unknown")
        if not isinstance(visibility, str) or visibility not in {"unknown", "shared", "node_local"}:
            raise StoragePlanningError("INVALID_STORAGE_ROOT", "Unsupported storage visibility.")
        formats = capabilities.get("formats", ["directory"])
        if not isinstance(formats, list) or len(formats) > 4 or any(not isinstance(f, str) or f not in {"directory", "archive", "sif", "squashfs"} for f in formats) or len(set(formats)) != len(formats):
            raise StoragePlanningError("INVALID_STORAGE_ROOT", "Unsupported storage format.")
        quota_reporting = capabilities.get("quota_reporting", "unknown")
        if not isinstance(quota_reporting, str) or quota_reporting not in {"unknown", "unsupported", "available"}:
            raise StoragePlanningError("INVALID_STORAGE_ROOT", "Unsupported quota capability.")
        filesystem_id = item.get("filesystem_id")
        if filesystem_id is not None and (not isinstance(filesystem_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", filesystem_id)):
            raise StoragePlanningError("INVALID_STORAGE_ROOT", "Invalid protected filesystem identity.")
        result.append({"alias": alias, "path": canonical_root(item.get("path")), "roles": list(roles),
                       "capabilities": {"visibility": capabilities.get("visibility", "unknown"), "formats": list(formats),
                                        "quota_reporting": capabilities.get("quota_reporting", "unknown")},
                       "filesystem_id": filesystem_id})
    return result


def roots_from_metadata(profile):
    if "storage_roots" in profile:
        return validate_storage_roots(profile["storage_roots"])
    return validate_storage_roots([
        {"alias": f"staging-root-{index}", "path": canonical_root(path), "roles": list(ROLES),
         "capabilities": {"visibility": "unknown", "formats": ["directory"], "quota_reporting": "unknown"}}
        for index, path in enumerate(profile.get("remote_root_prefixes", [])[:32])])


def default_health_policy():
    return {"warning_percent": 10, "stop_percent": 5, "emergency_percent": 2, "recovery_percent": 7,
            "warning_floor_bytes": 1024**4, "stop_floor_bytes": 500 * 1024**3,
            "emergency_floor_bytes": 200 * 1024**3, "recovery_floor_bytes": 700 * 1024**3,
            "warning_floor_inodes": 0, "stop_floor_inodes": 0, "emergency_floor_inodes": 0,
            "recovery_floor_inodes": 0, "freshness_seconds": 120, "recovery_seconds": 300}


def validate_health_policy(value):
    defaults = default_health_policy()
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise StoragePlanningError("INVALID_STORAGE", "Invalid filesystem health policy.")
    policy = {**defaults, **value}
    for number in policy.values():
        integer(number)
    if not (0 <= policy["emergency_percent"] < policy["stop_percent"] < policy["recovery_percent"] < policy["warning_percent"] <= 100):
        raise StoragePlanningError("INVALID_STORAGE", "Health percentages must have ordered recovery thresholds.")
    for unit in ("bytes", "inodes"):
        if not (policy[f"emergency_floor_{unit}"] <= policy[f"stop_floor_{unit}"] <= policy[f"recovery_floor_{unit}"] <= policy[f"warning_floor_{unit}"]):
            raise StoragePlanningError("INVALID_STORAGE", "Health floors must have ordered recovery thresholds.")
    if not 1 <= policy["freshness_seconds"] <= 3600 or not 1 <= policy["recovery_seconds"] <= 86400:
        raise StoragePlanningError("INVALID_STORAGE", "Invalid freshness or recovery interval.")
    return policy


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise StoragePlanningError("INVALID_STORAGE", "A timezone-aware observation time is required.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise StoragePlanningError("INVALID_STORAGE", "Invalid observation time.") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise StoragePlanningError("INVALID_STORAGE", "Observation times must include a timezone.")
    return result


def validate_observation(document, *, now=None):
    """Wire contract for a future fixed worker probe; never trusts exception text."""
    now = now or datetime.now(timezone.utc)
    fields = {"schema_version", "observed_at", "source", "filesystem_id", "identity_verified", "status",
              "total_bytes", "available_bytes", "total_inodes", "available_inodes", "quota", "error_code"}
    if not isinstance(document, dict) or set(document) - fields:
        raise StoragePlanningError("INVALID_STORAGE", "Unexpected capacity observation fields.")
    if document.get("schema_version") != "1.0" or not isinstance(document.get("source"), str) or document.get("source") not in {"statvfs", "site_probe"}:
        raise StoragePlanningError("INVALID_STORAGE", "Unsupported capacity observation source.")
    observed = timestamp(document.get("observed_at"))
    if observed > now + timedelta(seconds=30):
        raise StoragePlanningError("INVALID_STORAGE", "Observation is in the future.")
    if not isinstance(document.get("status"), str) or document.get("status") not in {"available", "unavailable"} or type(document.get("identity_verified")) is not bool:
        raise StoragePlanningError("INVALID_STORAGE", "Invalid observation status.")
    identity = document.get("filesystem_id")
    if identity is not None and (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", identity)):
        raise StoragePlanningError("INVALID_STORAGE", "Invalid filesystem identity.")
    if document["identity_verified"] and not identity:
        raise StoragePlanningError("INVALID_STORAGE", "A verified filesystem must have an identity.")
    result = {**document}
    for key in ("total_bytes", "available_bytes", "total_inodes", "available_inodes"):
        result[key] = integer(document.get(key), nullable=True)
    for unit in ("bytes", "inodes"):
        capacity, available = result[f"total_{unit}"], result[f"available_{unit}"]
        if capacity is not None and available is not None and available > capacity:
            raise StoragePlanningError("INVALID_STORAGE", "Available capacity exceeds filesystem capacity.")
        if capacity == 0:
            result[f"total_{unit}"] = result[f"available_{unit}"] = None
    quota = document.get("quota", {"status": "unknown"})
    quota_fields = {"status", "limit_bytes", "used_bytes", "available_bytes", "limit_inodes", "used_inodes", "available_inodes"}
    if not isinstance(quota, dict) or set(quota) - quota_fields or not isinstance(quota.get("status"), str) or quota.get("status") not in {"reported", "unknown", "unavailable", "unsupported"}:
        raise StoragePlanningError("INVALID_STORAGE", "Invalid quota observation.")
    result["quota"] = {"status": quota["status"], **{key: integer(quota.get(key), nullable=True) for key in quota_fields - {"status"}}}
    for unit in ("bytes", "inodes"):
        limit, available = result["quota"][f"limit_{unit}"], result["quota"][f"available_{unit}"]
        if limit is not None and available is not None and available > limit:
            raise StoragePlanningError("INVALID_STORAGE", "Reported quota availability exceeds its limit.")
    error = document.get("error_code", "")
    if not isinstance(error, str) or error and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", error):
        raise StoragePlanningError("INVALID_STORAGE", "Use a bounded generic probe error code.")
    result["error_code"] = error
    return result


def threshold(policy, severity, unit, capacity):
    return max(policy[f"{severity}_floor_{unit}"], (capacity or 0) * policy[f"{severity}_percent"] // 100)


def assess_health(observation, policy=None, *, now=None, previous_state="unknown", recovery_since=None, previous_observed_at=None):
    now = now or datetime.now(timezone.utc)
    policy = validate_health_policy(policy or {})
    result = {"state": "unknown", "observed_at": None, "age_seconds": None, "recovery_since": None,
              "unknowns": [], "policy": policy, "advisory_only": True}
    if not observation:
        result["unknowns"] = ["capacity_not_observed", "quota_not_observed"]
        return result
    data = validate_observation(observation, now=now)
    observed = timestamp(data["observed_at"])
    result.update(observed_at=data["observed_at"], age_seconds=max(0, int((now - observed).total_seconds())))
    if result["age_seconds"] > policy["freshness_seconds"]:
        result["state"] = "stale"
        result["unknowns"].append("capacity_observation_stale")
        return result
    if data["status"] != "available":
        result["state"] = "unavailable"
        return result
    if not data["identity_verified"]:
        result["unknowns"].append("filesystem_identity_not_verified")
        return result
    levels = []
    for unit in ("bytes", "inodes"):
        capacity, available = data[f"total_{unit}"], data[f"available_{unit}"]
        if capacity in (None, 0) or available is None:
            result["unknowns"].append(f"filesystem_{unit}_unknown")
            continue
        levels.append(next((state for severity, state in [("emergency", "emergency"), ("stop", "critical"), ("warning", "pressure")]
                            if available < threshold(policy, severity, unit, capacity)), "normal"))
    if data["quota"]["status"] != "reported":
        result["unknowns"].append("native_quota_unknown")
    rank = {"normal": 0, "pressure": 1, "critical": 2, "emergency": 3}
    result["state"] = max(levels, key=rank.get) if levels else "unknown"
    physical_unknown = any(item.startswith("filesystem_") for item in result["unknowns"])
    if physical_unknown and result["state"] == "normal":
        result["state"] = "unknown"
    if physical_unknown and previous_state in {"critical", "emergency", "recovering"}:
        result["state"] = "critical"
    if previous_state in {"critical", "emergency", "recovering"} and result["state"] in {"normal", "pressure"}:
        recovered = all(data[f"available_{unit}"] is not None and data[f"total_{unit}"] not in (None, 0)
                        and data[f"available_{unit}"] >= threshold(policy, "recovery", unit, data[f"total_{unit}"])
                        for unit in ("bytes", "inodes") if f"filesystem_{unit}_unknown" not in result["unknowns"])
        if not recovered:
            result["state"] = "critical"
        else:
            if previous_observed_at and (observed - timestamp(previous_observed_at)).total_seconds() > policy["freshness_seconds"]:
                recovery_since = None
            since = timestamp(recovery_since) if recovery_since else now
            result["recovery_since"] = since.isoformat()
            if (now - since).total_seconds() < policy["recovery_seconds"]:
                result["state"] = "recovering"
    return result


def validate_plan(document):
    if not isinstance(document, dict) or set(document) - {"schema_version", "connection_id", "estimates"} or document.get("schema_version", "1.0") != "1.0":
        raise StoragePlanningError("INVALID_STORAGE_PLAN", "Expected a versioned storage plan.")
    try:
        connection_id = str(UUID(document["connection_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise StoragePlanningError("INVALID_STORAGE_PLAN", "Select a cluster identifier.") from None
    estimates, roles, normalized = document.get("estimates"), set(), []
    if not isinstance(estimates, list) or not 1 <= len(estimates) <= len(ROLES):
        raise StoragePlanningError("INVALID_STORAGE_PLAN", "Provide one to five role estimates.")
    for item in estimates:
        if not isinstance(item, dict) or set(item) - {"role", "target_id", "peak_bytes", "peak_inodes", "retained_bytes", "retained_inodes"}:
            raise StoragePlanningError("INVALID_STORAGE_PLAN", "Unexpected estimate fields; paths are not accepted.")
        role = item.get("role")
        if role not in ROLES or role in roles:
            raise StoragePlanningError("INVALID_STORAGE_PLAN", "Storage roles must be supported and distinct.")
        roles.add(role)
        try:
            target_id = str(UUID(item["target_id"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise StoragePlanningError("INVALID_STORAGE_PLAN", "Select an approved target identifier.") from None
        entry = {"role": role, "target_id": target_id}
        for unit in ("bytes", "inodes"):
            peak = integer(item.get(f"peak_{unit}"))
            retained = integer(item.get(f"retained_{unit}", 0))
            if retained > peak:
                raise StoragePlanningError("INVALID_STORAGE_PLAN", "Retained estimates cannot exceed peak growth.")
            entry.update({f"peak_{unit}": peak, f"retained_{unit}": retained})
        normalized.append(entry)
    return {"schema_version": "1.0", "connection_id": connection_id, "estimates": normalized}


def plan_storage(document, inventory, *, now=None):
    """Assess estimates against a caller-filtered snapshot, without reservations."""
    now = now or datetime.now(timezone.utc)
    request = validate_plan(document)
    targets = {target["id"]: target for target in inventory["targets"]}
    binding = next((item for item in inventory["bindings"] if item["connection_id"] == request["connection_id"]), None)
    if binding is None:
        raise StoragePlanningError("STORAGE_TARGET_DENIED", "No storage locations are configured for this cluster.")
    grouped = {}
    for item in request["estimates"]:
        target = targets.get(item["target_id"])
        if not target or binding["targets"].get(item["role"]) != item["target_id"] or item["role"] not in target["roles"]:
            raise StoragePlanningError("STORAGE_TARGET_DENIED", "Use the project's selected target for this role and cluster.")
        group = grouped.setdefault(target["pool_id"], {"peak_bytes": [], "peak_inodes": []})
        for unit in ("bytes", "inodes"):
            group[f"peak_{unit}"].append(item[f"peak_{unit}"])
    budget = inventory["budget"]
    reasons, unknowns, pools = [], [], []
    estimates = {f"peak_{unit}": total(item[f"peak_{unit}"] for item in request["estimates"]) for unit in ("bytes", "inodes")}
    estimates.update({f"retained_{unit}": total(item[f"retained_{unit}"] for item in request["estimates"]) for unit in ("bytes", "inodes")})
    for unit in ("bytes", "inodes"):
        projected = total([budget[f"retained_{unit}_estimate"], estimates[f"peak_{unit}"]])
        estimates[f"projected_{unit}_estimate"] = projected
        if budget[f"{unit}_budget"] is not None and projected > budget[f"{unit}_budget"]:
            reasons.append(f"project_{unit}_budget_exceeded")
    if budget["estimate_updated_at"] is None:
        unknowns.append("project_retained_usage_not_estimated")
    pool_index = {pool["id"]: pool for pool in inventory["pools"]}
    for pool_id, peaks in grouped.items():
        pool = pool_index[pool_id]
        health = assess_health(pool["observation"], pool["health_policy"], now=now,
                               previous_state=pool["health_state"], recovery_since=pool["recovery_since"])
        peak = {key: total(values) for key, values in peaks.items()}
        row = {"pool_id": pool_id, "health": health, **peak, "headroom_bytes_estimate": None, "headroom_inodes_estimate": None}
        unknowns.extend(f"{pool_id}:{item}" for item in health["unknowns"])
        if health["state"] in {"critical", "emergency", "recovering"}:
            reasons.append(f"{pool_id}:filesystem_{health['state']}")
        if health["state"] in {"unknown", "stale", "unavailable"}:
            unknowns.append(f"{pool_id}:capacity_not_current")
        else:
            observation = validate_observation(pool["observation"], now=now)
            for unit in ("bytes", "inodes"):
                available = observation.get(f"available_{unit}")
                quota = observation.get("quota", {})
                quota_available = quota.get(f"available_{unit}") if quota.get("status") == "reported" else None
                if available is None:
                    unknowns.append(f"{pool_id}:{unit}_available_unknown")
                    continue
                capacity = observation.get(f"total_{unit}")
                floor = threshold(health["policy"], "stop", unit, capacity)
                required = total([peak[f"peak_{unit}"], floor])
                row[f"headroom_{unit}_estimate"] = available - required
                if row[f"headroom_{unit}_estimate"] < 0:
                    reasons.append(f"{pool_id}:{unit}_headroom_insufficient")
                if quota_available is not None and quota_available < peak[f"peak_{unit}"]:
                    reasons.append(f"{pool_id}:reported_quota_{unit}_insufficient")
        pools.append(row)
    return {"schema_version": "1.0", "project_id": inventory["project_id"], "connection_id": request["connection_id"],
            "dry_run": True, "advisory_only": True, "creates_reservations": False, "enforced_at_dispatch": False,
            "status": "blocked" if reasons else "unknown" if unknowns else "warning" if any(p["health"]["state"] == "pressure" for p in pools) else "ready",
            "reasons": sorted(set(reasons)), "unknowns": sorted(set(unknowns)), "estimates": estimates, "pools": pools,
            "note": "Planning estimates do not enforce filesystem quotas or redirect existing jobs. External writes and undeclared job growth can exceed them."}
