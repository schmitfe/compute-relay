"""Parse read-only Slurm facts without treating visibility as authorization."""

from __future__ import annotations

import datetime as dt
import re


def _number(value: str | None, multiplier: int = 1) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d+)(?:\s+(?:sec|min|seconds|minutes))?", value.strip(), re.IGNORECASE)
    return int(match.group(1)) * multiplier if match else None


def parse_discovery(response: dict, connection: dict) -> dict:
    nodes = []
    for line in response.get("nodes", {}).get("stdout", "").splitlines():
        parts = line.split("|")
        if len(parts) == 6:
            node = dict(zip(("name", "partition", "cpus", "memory_mib", "gres", "state"), parts))
            node["partition"] = node["partition"].removesuffix("*")
            nodes.append(node)
    partitions = []
    for line in response.get("partitions", {}).get("stdout", "").splitlines():
        values = {match.group(1): match.group(2) for match in re.finditer(r"(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=([^\s]*)", line)}
        if "PartitionName" in values:
            partitions.append(values)
    settings = {}
    for line in response.get("settings", {}).get("stdout", "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            settings[key.strip()] = value.strip()
    associations = []
    for line in response.get("associations", {}).get("stdout", "").splitlines():
        fields = line.split("|")
        if len(fields) >= 5:
            associations.append(dict(zip(("user", "account", "partition", "qos", "default_qos"), fields[:5])))
    profile = connection.get("site_profile", {})
    cleanup = profile.get("cleanup_margin_seconds")
    if type(cleanup) is not int or cleanup < 0:
        cleanup = None
    timing = {}
    for partition in partitions:
        termination = _number(settings.get("KillWait"))
        partition_overtime = partition.get("OverTimeLimit")
        # Slurm23.11 partition_info.c prints NONE for NO_VAL16; job_mgr.c
        # inherits the global limit for that sentinel. UNLIMITED is distinct.
        # https://github.com/SchedMD/slurm/blob/slurm-23-11-4-1/src/api/partition_info.c#L315
        # https://github.com/SchedMD/slurm/blob/slurm-23-11-4-1/src/slurmctld/job_mgr.c#L9652
        overtime_value = settings.get("OverTimeLimit") if partition_overtime is None or partition_overtime.upper() == "NONE" else partition_overtime
        overtime = _number(overtime_value, 60)
        finite = termination is not None and overtime is not None and cleanup is not None
        timing[partition["PartitionName"]] = {
            "termination_margin_seconds": termination,
            "overtime_margin_seconds": overtime,
            "overtime_source": "global" if partition_overtime is None or partition_overtime.upper() == "NONE" else "partition",
            "cleanup_margin_seconds": cleanup,
            "finite_timing_candidate": finite,
            "requires_operator_verification": True,
            "unbounded_requeue_risk": "ForceRequeueOnFail" in settings.get("PrologFlags", ""),
        }
    return {
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "connection_state": "ready",
        "version": response.get("version", {}).get("stdout", "").strip(),
        "nodes": nodes, "partitions": partitions, "scheduler_settings": settings,
        "associations": associations,
        "association_query_status": "available" if response.get("associations", {}).get("returncode") == 0 else "unknown",
        "accounting_comment_markers": "enabled" if "job_comment" in settings.get("AccountingStoreFlags", "").lower()
        else ("not_enabled" if "AccountingStoreFlags" in settings else "unknown"),
        "authorization": "unknown; visible resources and association snapshots require project/site authorization",
        "availability": "snapshot only; no resources reserved",
        "timing_candidates": timing,
        "software": response.get("software", {"status": "not_reported", "scope": "login_node", "verified_on_compute": False}),
        "notes": ["GPU types/counts are advertised GRES; GPU memory may require an operator annotation.",
                  "Cleanup charge duration cannot be inferred reliably from ordinary discovery; a finite verified site margin is required.",
                  "Discovery does not enable budgeted admission or submit compute-node probes."],
        "commands": {key: {"returncode": value.get("returncode"), "stderr": value.get("stderr", "")[:1024]}
                     for key, value in response.items() if isinstance(value, dict) and key != "software"},
    }


def discover(connection: dict, transport=None) -> dict:
    """Convenience entry point for an operator CLI; performs no submissions."""
    from .slurm import SlurmBackend

    return SlurmBackend(connection, transport=transport).discover()
