"""Parse read-only Slurm facts without treating visibility as authorization."""

from __future__ import annotations

import datetime as dt
import re


def parse_discovery(response: dict, connection: dict) -> dict:
    nodes = []
    for line in response.get("nodes", {}).get("stdout", "").splitlines():
        parts = line.split("|")
        if len(parts) in {6, 9}:
            node = dict(zip(("name", "partition", "cpus", "memory_mib", "gres", "state"), parts))
            if len(parts) == 9:
                node.update(dict(zip(("sockets", "cores_per_socket", "threads_per_core"), parts[6:])))
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
        "software": response.get("software", {"status": "not_reported", "scope": "login_node", "verified_on_compute": False}),
        "notes": ["GPU types/counts are advertised GRES; GPU memory may require an operator annotation.",
                  "Compute reservations use requested walltime; final accounting uses actual allocated elapsed time.",
                  "Discovery does not enable budgeted admission or submit compute-node probes."],
        "commands": {key: {"returncode": value.get("returncode"), "stderr": value.get("stderr", "")[:1024]}
                     for key, value in response.items() if isinstance(value, dict) and key != "software"},
    }


def discover(connection: dict, transport=None) -> dict:
    """Convenience entry point for an operator CLI; performs no submissions."""
    from .slurm import SlurmBackend

    return SlurmBackend(connection, transport=transport).discover()
