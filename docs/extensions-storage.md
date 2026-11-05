# Advisory storage planning

The bundled storage module records project locations, byte/inode budgets and
per-cluster defaults. Agents read configuration and make dry-run estimates.
It does not redirect job/build writes, reserve physical space, enforce OS quotas,
scan retained data or delete files. Enablement follows [extensions](extensions.md).

## Human configuration

Open `/projects/<id>/storage/` and select an assigned cluster. Each location is
project-owned, even when another project uses the same directory or filesystem.
Its connection owner must authorize filesystem access; project membership does
not grant someone else's SSH identity.

For personal connectors, enter a path or **Browse directories**, then:

1. **Check/create directory** queues a fixed worker operation under your cluster
   account. Missing parents are created privately (`0700`); existing directories
   retain contents/permissions. Files, final links and symlink substitutions fail.
2. Review the requested and canonical writable directory and **Save storage
   location** within five minutes. Checking alone never registers it.

Browsing/creation require human POSTs; GETs show status. Selections survive the
browser. Registration binds project/owner, task, canonical path and exact worker
profile/connection revision. Retries do not create another location. Replacing
that binding requires fresh registration. No agent browsing, creation or path-write
API is added.

Personal directories support advisory budgets/roles; their capacity, filesystem
identity and quota remain unknown. Capacity checks require a separately advertised
protected root. Managed workers select an advertised exact root;
`remote_root_prefixes` remain conservative choices with unknown quota/visibility.
Core profiles do not yet advertise the optional `storage_roots` metadata contract.
Do not broaden SSH roots merely to obtain a storage feature.

Locations bind `work`, `environments`, `data`, `results` or `scratch` roles for one
project/cluster. Enrollment fills only empty supported defaults. Stale, unassigned
or key-restricted locations are unavailable to plans. An owner may assign an
unassigned location only without conflicting project references; otherwise create
a separate location. No assignment moves/scans files.

Pools may group compatible locations on one cluster with matching protected
filesystem identities. Unknown identities do not prove a shared filesystem.
Sharing a pool shares neither project targets nor permissions/budgets. Manual
usage is timestamped `human_estimate`; free-space readings do not attribute
retained bytes to a project.

## Agent API

Both routes require `jobs:read` and enforce project/key cluster restrictions:

| Route | Result |
| --- | --- |
| `GET /api/v1/projects/<id>/storage` | Current targets, canonical roots, roles/defaults, budgets/estimates and cached pool observations |
| `POST /api/v1/projects/<id>/storage/plan` | At most five distinct role estimates; no jobs, writes or reservations |

MCP `get_project_storage` returns `bindings[].targets[role]`; resolve that ID in
`targets[]` and use its exact `root`, never its display name. This does not change
the job working directory. Copy/package wanted shared-location results into the
job's declared relative outputs for collection.

```json
{
  "schema_version": "1.0",
  "connection_id": "00000000-0000-4000-8000-000000000001",
  "estimates": [{
    "role": "environments",
    "target_id": "00000000-0000-4000-8000-000000000002",
    "peak_bytes": 10737418240,
    "peak_inodes": 100000,
    "retained_bytes": 2147483648,
    "retained_inodes": 1
  }]
}
```

A target must be this project's selected location for that role/cluster. Unknown
fields, paths/endpoints, duplicate roles, negative/fractional counters and integers
above `2^53-1` fail. Peak includes simultaneous staging, extraction, caches and
final trees; retained cannot exceed peak. Describe distinct growth per role to
avoid double counting; roles on the same pool sum conservatively.

Plans report `advisory_only`, `dry_run`, `creates_reservations: false` and
`enforced_at_dispatch: false`. `ready`, `warning`, `blocked` or `unknown` is a
planning assessment, not execution permission. Project budgets use retained plus
peak estimates. Pool headroom subtracts new peak and operational reserve from
free space, without subtracting retained bytes twice; native quota is separate.
Unrelated writes and other projects' pending growth remain unreserved.

## Opt-in capacity checks

Checks default off and require storage enabled in both control and worker, a
current supported protected root and explicit owner enablement. Pausing fences
in-flight leases; saving unrelated project settings never probes. Fake backends
report unavailable capacity.

One fixed probe per enabled pool is normally due each minute, with at most one
outstanding per worker. Completion yields the maintenance lane for 15 seconds;
unavailable readings back off five minutes. The worker resolves the leased alias
from its protected profile, never an agent/lease-supplied path or command.

`POST /internal/v1/extensions/storage/pools/<id>/report` authenticates the worker,
lease ID/generation, target and profile/connection digests. Leases last 90 seconds;
pause, changed bindings and newer generations reject stale reports. Identical
replays reuse receipts; conflicts fail. Observations advance within the lease,
allowing 30 seconds clock skew. Project keys cannot write observations.

Inventories distinguish `probe_wired`, `probe_enabled`, `probe_supported`, last
observation and next due time. Until an enabled supported worker reports, capacity
is unknown. The observation shape is:

```json
{
  "schema_version": "1.0", "source": "statvfs", "status": "available",
  "observed_at": "2026-10-21T12:00:00+00:00",
  "filesystem_id": "protected-volume-id", "identity_verified": true,
  "total_bytes": 60000000000000, "available_bytes": 6000000000000,
  "total_inodes": null, "available_inodes": null,
  "quota": {"status": "unknown"}
}
```

Sources are `statvfs`/`site_probe`; status is `available`/`unavailable`; unknown
counters are null. Quota status is `reported`, `unknown`, `unavailable` or
`unsupported`, with limit/used/available byte/inode counters when reported.
Errors contain bounded codes, never raw messages or credentials. Missing expected
filesystem identity stays unverified; mismatches become unavailable, protecting
against an unmounted NAS being mistaken for its local backing directory.

The fixed no-follow probe opens one approved root and reads `fstat`/`fstatvfs`:
no listings, contents, writes, programs or jobs. It has bounded requests/depth,
a two-second cooperative deadline and external SSH timeout up to 30 seconds for
blocking NFS calls. Available bytes exclude administrator-reserved blocks;
unsupported inode sentinels stay unknown and unrepresentable counters fail closed.
Without a pinned expected identity, a reading cannot attest the intended mount.
Native user quota is currently always unknown.

Observations expire after 120 seconds. Defaults warn below max(10%, 1 TiB), block
planned growth below max(5%, 500 GiB), and flag emergency below max(2%, 200 GiB).
Inode thresholds are separate. Critical pressure is immediate; recovery needs
five continuous minutes above 7%/700 GiB and the configured inode threshold.
These tunable estimates are not a job-dispatch gate or physical reservation.

## Disposable acceptance test

`deploy/compose.storage-test.yml` overlays the ordinary Slurm fixture, enabling
storage and mounting `tools/storage_probe_smoke.py` read-only. On an existing
isolated fixture, supply `--confirm-disposable-fixture`, synthetic `--demo-file`,
local `--base-url` and timeout at most 120 seconds. No institutional credentials.

The client verifies `brokerjob@slurm-fixture` and `/home/brokerjob/jobs`, enrolls
and selects a project target, issues a read-only key, explicitly enables checks,
waits for a new byte/inode reading and tests planning. Quota/unpinned identity stay
unknown, admitted compute stays zero, and the pool is paused afterward. It reads
no SSH credentials or remote file contents and requests no writes/scans/jobs.
Small fixture filesystems may correctly report blocked plans under default floors.
