"""Fixed metadata-only probe building blocks for a future protected worker hook.

Nothing calls this module in the shipped extension. Callers resolve an enrolled
root themselves and run the probe in a process with an external timeout: an NFS
system call can block beyond Python's cooperative deadline. No directory scan,
file-content read, subprocess, create, mount, or deletion occurs here.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from .planning import StoragePlanningError, canonical_root, integer, validate_observation


def snapshot_from_statvfs(stats, *, filesystem_id, expected_filesystem_id=None, observed_at=None, quota=None):
    """Pure conversion. Zero/unsupported inode totals become unknown, not full."""
    observed_at = observed_at or datetime.now(timezone.utc)
    fragment = integer(stats["f_frsize"])
    total_blocks, available_blocks = integer(stats["f_blocks"]), integer(stats["f_bavail"])
    total_inodes, available_inodes = stats.get("f_files"), stats.get("f_favail")
    if type(total_inodes) is not int or total_inodes <= 0 or type(available_inodes) is not int or available_inodes < 0:
        total_inodes = available_inodes = None
    verified = bool(expected_filesystem_id and filesystem_id == expected_filesystem_id)
    snapshot = {"schema_version": "1.0", "observed_at": observed_at.isoformat(), "source": "statvfs",
                "status": "available", "filesystem_id": filesystem_id, "identity_verified": verified,
                "total_bytes": integer(total_blocks * fragment), "available_bytes": integer(available_blocks * fragment),
                "total_inodes": total_inodes, "available_inodes": available_inodes,
                "quota": quota or {"status": "unknown"}}
    if expected_filesystem_id and not verified:
        snapshot.update(status="unavailable", error_code="FILESYSTEM_IDENTITY_MISMATCH")
    return validate_observation(snapshot, now=observed_at)


def probe_filesystem(root, *, expected_filesystem_id=None, timeout_seconds=2, observed_at=None):
    """Read one approved root's filesystem statistics via no-follow directory FDs.

    This is not an agent-facing path API. The future worker must pass a root from
    its own protected target map and enforce a hard process timeout externally.
    """
    root = canonical_root(root)
    parts = root[1:].split("/")
    if len(parts) > 64 or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 10:
        raise StoragePlanningError("INVALID_STORAGE_PROBE", "Probe bounds exceeded.")
    deadline = time.monotonic() + timeout_seconds
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if time.monotonic() >= deadline:
                raise TimeoutError("Storage probe deadline exceeded")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info, stats = os.fstat(descriptor), os.fstatvfs(descriptor)
        if time.monotonic() >= deadline:
            raise TimeoutError("Storage probe deadline exceeded")
        return snapshot_from_statvfs({key: getattr(stats, key) for key in ("f_frsize", "f_blocks", "f_bavail", "f_files", "f_favail")},
            filesystem_id=f"{info.st_dev}:{stats.f_fsid}", expected_filesystem_id=expected_filesystem_id, observed_at=observed_at)
    finally:
        os.close(descriptor)
