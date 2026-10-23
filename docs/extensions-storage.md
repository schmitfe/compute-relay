# Optional advisory storage planning

Enable the storage extension with `BROKER_EXTENSIONS=storage` in an installation
that includes its package, then apply its Django migrations. It is disabled by
default. The extension has its own application, models, migrations, URLs and
templates; core job preparation, admission and accounting do not require it.

This implementation saves approved locations, shared project byte/inode budgets,
and a per-cluster choice of work, environment, results and scratch locations. Its
agent API reads configuration and performs **dry-run estimates only**. It does
not redirect existing jobs, reserve physical disk space, enforce OS quotas,
scan retained data, build images, extract archives or delete data. Optional,
human-enabled capacity checks read filesystem statistics through the protected
worker; they do not measure a project's actual retained usage.

## Human configuration

Open `/storage/targets/` to enroll locations. For a personal connector, the cluster
owner can enter a dedicated directory or use **Browse directories** to inspect
directory names and access metadata through their running worker. Browsing is
requested by human POSTs, and GET pages only display task status. Form selections
are retained when returning from the browser.

Select **Check/create directory** to queue one fixed connector operation. If the
final component is missing, the worker creates that one private directory under
an existing parent; missing parents, existing files and final symbolic links are
rejected. Existing directories keep their contents and permissions. The check
expires after five minutes and does not register the location automatically.
Review its result and select **Save storage location**. The review shows both the
requested path and the worker's canonical directory when a parent alias resolves
elsewhere. Saving explicitly confirms that displayed canonical path. Registration binds the
owner, exact task and returned writable directory, worker profile fingerprint and
connection revision. Repeating a completed form does not create another task or
location. Replacing a worker or changing its execution configuration requires a current binding.

Personal directories support advisory project budgets and role selection while
free capacity, filesystem identity and native quota remain unknown. Read-only
capacity probes are unavailable for this provenance until the protected worker
separately publishes a supported root. This configuration neither expands job
staging authorization nor redirects existing jobs, and introduces no agent path,
URL, browsing, creation or filesystem-write API.

Managed workers retain the existing path: a human selects one exact root already
advertised by the protected worker profile. Existing `remote_root_prefixes` are
supported as conservative compatibility choices with unknown visibility and
quota capabilities.

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

Capacity checks are **off by default**, including for existing targets after
migration. For supported protected roots, an owner can explicitly enable them while enrolling a location or
using its enable/pause button at `/storage/targets/`. Pausing invalidates in-flight
leases. Saving project settings does not trigger checks. Probes do not scan,
create or change remote files. The separate human directory-creation step above
creates only its requested final directory. Manual usage values are labeled
`human_estimate`, with their update time; they are not filesystem measurements.

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

## Opt-in capacity checks

The control and worker installations must both enable the storage extension.
The worker advertises that capability before it can receive maintenance leases.
For each enabled capacity pool, the control service issues one fixed probe of
the pool's approved root, normally once a minute. One outstanding probe is
allowed per worker, a completed check yields the maintenance lane for at least
15 seconds, and unavailable results back off for five minutes. This gives core
job observations and accounting opportunities between maintenance calls.

The extension implements the generic `services.lease_work(worker)` maintenance
hook. Its context contains pool/target/connection IDs, a root alias and exact
protected profile/connection revision digests. The worker resolves the actual
path from its own protected profile. Neither an agent nor a lease can introduce
an arbitrary path, endpoint, SSH option, program or script.

Reports go to the worker-authenticated
`POST /internal/v1/extensions/storage/pools/<pool-id>/report`. A report binds the
lease ID, generation, target and both revision digests to its observation. Leases
expire after 90 seconds; pause, configuration changes and newer generations fence
old results. Identical report retries return a saved receipt; conflicting retries
are rejected. Timestamps must advance and belong to the current lease, allowing
30 seconds of clock skew. There is **no project bearer observation-write API**.

Inventories report `probe_wired: true` separately from each pool's
`probe_enabled`, `probe_supported`, cached observation timestamp and next due
time. Until an owner enables checks and a supported worker returns a result,
capacity remains unknown. Fake backends return explicitly unavailable readings;
they never fabricate capacity.

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

`remote.py` supplies the fixed, standalone probe source executed through the
existing protected SSH transport. It opens one approved root through no-follow
directory descriptors and reads `fstat`/`fstatvfs` metadata, without directory
listing, file contents, writes, job submission or additional programs. Requests,
depth and counters are bounded. A two-second cooperative deadline is paired with
an external SSH timeout of at most 30 seconds because an NFS syscall can block
beyond a Python deadline. `probe.py` also provides pure conversion helpers for
local tests and future adapters.

Available bytes use the filesystem's user-available count, rather than reserved
blocks available only to an administrator. Unsupported or out-of-range inode
sentinels become unknown. Counters above `2^53-1` are not representable by this
wire contract and otherwise fail closed to unavailable. The reported identity
combines the device and filesystem IDs; without an operator-pinned expected
identity it remains explicitly unverified. Current staging-prefix compatibility
metadata has no identity pin, so a successful reading alone does not establish
that the intended NAS is mounted. User/group/project quota adapters remain
site-specific future work; global free space does not establish a user's
remaining quota. The fixed probe always reports native quota as unknown.

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

Wire job write-path derivation before treating locations as execution
destinations. Add authenticated streaming transfer and
explicit storage quotas appropriate for large scientific package/data objects.
On inode-limited PFS, an operator-verified read-only SIF/SquashFS execution profile
can retain environments as single files while builds use node-local scratch.
Archives alone do not make virtual environments relocatable, prevent expanded
inode use, or protect against decompression bombs. Safe extraction, packed-image
builds, retention policies, pinned-object garbage collection, and storage secret
profiles remain future features. Changing an object's mode does not make it
immutable against its owning remote UID. A read-only execution mount also does
not prevent that UID from modifying an image through another writable path.

## Disposable acceptance test

`tools/storage_probe_smoke.py` uses the existing bounded public HTTP client and
requires `--confirm-disposable-fixture`. Run it against an already-started Docker
Slurm fixture with storage enabled on control and worker. Supply the fixture's
synthetic `demo.json` using `--demo-file`, its local control origin using
`--base-url`, and a timeout of at most 120 seconds.
`deploy/compose.storage-test.yml` is an overlay for `deploy/compose.test.yml` that
enables storage on control and worker, mounts just this client read-only and
selects the explicit fixture-confirmation arguments. The base smoke service's
demo-metadata volume supplies the synthetic credentials; no new stack or secret
mount is needed when using an already-running fixture project.

The client refuses nonfixture demo metadata and checks the actual selected
cluster page for `brokerjob@slurm-fixture`. It requires the exact protected
`/home/brokerjob/jobs` choice, enrolls a target through the owner form, selects it
for a new project, issues a `jobs:read` key, and explicitly enables capacity
checks through the session/CSRF route. It waits for a new real byte/inode reading,
checks that native quota and unpinned filesystem identity remain unknown, runs
advisory planning, and verifies zero admitted jobs and zero compute accounting.
It pauses the pool afterward. The client never reads SSH credentials or requests
a remote file write, job, directory listing or retained-usage scan. Small fixture
filesystems may correctly produce a blocked planning assessment under the
default operational free-space floors.
