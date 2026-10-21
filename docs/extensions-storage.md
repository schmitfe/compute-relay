# Optional advisory storage planning

Enable the storage extension with `BROKER_EXTENSIONS=storage` in an installation
that includes its package, then apply its Django migrations. It is disabled by
default. The extension has its own application, models, migrations, URLs and
templates; core job preparation, admission and accounting do not require it.

This implementation saves approved locations, shared project byte/inode budgets,
and a per-cluster choice of work, environment, results and scratch locations. Its
agent API reads configuration and performs **dry-run estimates only**. It does
not redirect existing jobs, reserve physical disk space, enforce OS quotas,
collect remote usage, build images, extract archives or delete data.

## Human configuration

Open `/storage/targets/` to enroll locations. A human owner chooses an owned
cluster and one exact root already advertised by its current protected worker
profile. There is no arbitrary-path or URL input. Existing `remote_root_prefixes`
are supported as conservative compatibility choices with unknown visibility and
quota capabilities. Root selection does not expand SSH staging authorization.

The plugin also validates an optional `storage_roots` metadata contract for a
future generic worker metadata extension: each entry has an `alias`, canonical
absolute `path`, supported `roles`, optional `filesystem_id`, and restricted
capabilities (`visibility`, `formats`, `quota_reporting`). Current core profile
publication does not yet advertise that optional field. Do not broaden SSH roots
just to enable unrelated storage: a protected storage metadata hook is the next
integration step.

Targets bind their exact worker profile fingerprint and connection revision.
Changing those bindings makes old targets unavailable for new plans. Enroll the
new binding; historical planning records remain. A capacity pool can group
locations on the same cluster only when their protected filesystem identities
agree, including explicitly unknown identities. Unknown grouping is not proof
that locations share a physical filesystem.

Project owners/admins configure `/projects/<project-id>/storage/`. Budgets and
manual retained-usage estimates apply across the whole project and all its API
keys. Location choices are separate for each assigned cluster. An enrolled
location is usable only by its owner's projects on the same cluster. Key cluster
restrictions narrow the API inventory and dry-run access further.

Saving a location or project setting does not probe, scan, create or change any
remote file. Manual usage values are labeled `human_estimate`, with their update
time; they are not filesystem measurements.

## Agent API

Both routes require a valid project bearer key with `jobs:read`:

- `GET /api/v1/projects/<project-id>/storage` returns allowed target IDs, roles,
  capabilities, selected locations, shared budgets, pending advisory estimates,
  and cached pool/quota observations. Agent responses omit absolute roots.
- `POST /api/v1/projects/<project-id>/storage/plan` accepts up to four distinct
  role estimates. It is read-only, needs no idempotency key, and creates no jobs,
  approvals, reservations or files.

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

The target must be the project's selected location for that role and cluster.
Unknown fields, paths, endpoints, duplicate roles, negative/fractional counters,
and integers above `2^53-1` are rejected. Peak estimates include simultaneous
staging, extraction/build trees, caches and final images; roles should describe
separate growth to avoid double counting. Retained estimates cannot exceed peak
growth. Multiple roles on one pool are summed conservatively.

Responses identify `advisory_only: true`, `dry_run: true`,
`creates_reservations: false`, and `enforced_at_dispatch: false`. `ready`,
`warning`, `blocked` and `unknown` describe a planning assessment, not permission
to execute or a guarantee of free disk space. Project budgets use retained plus
held plus proposed peak estimates. Pool headroom subtracts other pending growth
and operational reserve from current free space; already-retained bytes are not
subtracted a second time. Available native quota is considered separately.

## Capacity integration hook

`services.record_observation(worker, pool_id, document)` is an internal hook for
a future authenticated fixed worker operation. There is **no public observation
write API**. It checks worker/connection scope, current protected target binding,
strictly advancing timestamps and safe counter values. No fabricated free-space
readings are inserted. Until the worker probe is wired, inventories explicitly
report `probe_wired: false` and missing observations as unknown.

The versioned observation contract is:

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

Source is `statvfs` or `site_probe`. Status is `available` or `unavailable`.
Unknown counters are null. Quota status is `reported`, `unknown`, `unavailable`
or `unsupported`; reported counters are `limit_bytes`, `used_bytes`,
`available_bytes`, `limit_inodes`, `used_inodes`, `available_inodes`.
`error_code` may contain a bounded uppercase code, never an exception message,
credential, command or URL. Missing expected filesystem identity forces the
observation to remain unverified. A mismatch becomes unavailable; this prevents
mistaking an unmounted directory's backing local filesystem for the NAS.

`probe.py` supplies a pure `snapshot_from_statvfs` converter and an unused,
metadata-only `probe_filesystem` building block. The latter opens one protected
root through no-follow directory descriptors and reads filesystem statistics.
It has depth/size/cooperative-time bounds and performs no recursive scan, content
read, write or subprocess. An integrating worker must resolve roots privately
and impose an external process timeout because an NFS syscall can block beyond
a Python deadline. User/group/project quota adapters remain site-specific future
work; global free space does not establish a user's remaining quota.

Default health policy warns below the greater of 10% or 1 TiB free, stops planned
new growth below 5% or 500 GiB, and marks emergency pressure below 2% or 200 GiB.
Inode percentage thresholds are separate. Observations expire after 120 seconds.
Critical pressure enters immediately; recovery requires five minutes continuously
above 7% / 700 GiB and the configured inode recovery threshold. These are
operator-tunable starting values, not universal storage guarantees. No job
dispatch gate is connected in this implementation.

`reserve_estimate` / `release_estimate` provide an internal, idempotent advisory
ledger for future integration. No agent endpoint creates these records. A caller
must prove its actual write path is derived from the selected target first. In
particular, the current environment-build attempt directory must not be charged
to an unrelated environment location selected in this UI. Shared remote Unix
UIDs and external writes remain outside the broker's isolation/enforcement.

## Further implementation

Wire fixed target-ID-based probes and job write-path derivation before treating
locations as execution destinations. Add authenticated streaming transfer and
explicit storage quotas appropriate for large scientific package/data objects.
On inode-limited PFS, an operator-verified read-only SIF/SquashFS execution profile
can retain environments as single files while builds use node-local scratch.
Archives alone do not make virtual environments relocatable, prevent expanded
inode use, or protect against decompression bombs. Safe extraction, packed-image
builds, retention policies, pinned-object garbage collection, and storage secret
profiles remain future features. A file owned by the same remote UID is mutable
even if its mount is read-only or its mode is changed.
