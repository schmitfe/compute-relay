# Implemented local runtime

Compute Relay is a single-user Python package. The normal command is
`compute-relay` (equivalently `compute-relay start`). It runs a Django portal
through Waitress on loopback, maintains a durable SQLite ledger and local object
store, and supervises saved SSH connectors in the foreground. Use a
[tested Python version](testing.md#ci) and system OpenSSH for real cluster access.

## Startup and persistent state

The default URL is `http://127.0.0.1:8768`. State defaults to
`$XDG_DATA_HOME/compute-relay`, or `~/.local/share/compute-relay`. Override the
location with `--data-directory PATH`. The local account is `local`; first setup
supports either a password or explicitly chosen passwordless access. Later
starts preserve that choice, budgets, job records, artifacts and connector
configuration. See [installation](deployment.md) for the complete flags.

The launcher starts only connectors saved for its installation. OpenSSH asks for
key passphrases and MFA in the terminal, one cluster at a time. A browser start
request queues that local operation; it cannot provide an SSH password or key.
Ctrl-C shuts down the portal and connectors without cancelling cluster jobs.
Restarting resumes the same durable state after any necessary SSH login.

The ordinary package includes Django, JSON Schema validation, RFC 8785 manifest
canonicalization, HTTPX, the official MCP SDK, and Waitress. The `test` extra adds
development checks. `uv.lock` pins development, CI and disposable fixture
installs; uv 0.13.6 checks the lock before syncing or running those checks.
Setuptools remains the package build backend. Normal tool or wheel installation
needs no extras, and the installed application does not require uv. See
[installation](deployment.md) and [development checks](testing.md).

### Diagnosing an unresponsive portal

The portal subprocess accepts `SIGUSR1` to append every Python thread's current
stack to the installation's private `web.log` without stopping the server. This
is available to web processes started with this version; it exposes no HTTP
diagnostic endpoint and prints no frame-local values.

The launcher also checks the identity-bound health endpoint every 15 seconds.
After three consecutive failed checks it requests one stack dump from its own
web child, leaving the service and jobs running. It captures another dump only
after health has recovered and a new sustained outage starts. Dumps stay in
private `web.log`; the launcher does not signal a PID read from a request or a
stale runtime file. This preserves evidence for the unresolved stall rather
than treating a restart as a fix.

Read the launcher's PID from the installation's private `runtime.json`. On Linux,
`ps -o pid=,args= --ppid LAUNCHER_PID` identifies its web subprocess running
`-m compute_relay.local_web`. Replace the PID placeholders with those verified
process IDs, then run `kill -USR1 WEB_PID`. Inspect the newly appended stacks in
that installation's `web.log` before restarting. Send the signal only to that web
subprocess: the launcher, connectors and older web processes do not install this
handler and can terminate if signalled. Keep logs private when sharing diagnostics.

The 2026-10-28 cluster run encountered a portal stall lasting at least four
minutes: all four request threads stopped responding, health checks timed out,
and the queued-request count grew to 24. Restarting restored service. No stacks
were captured during the stall, so its cause remains unconfirmed.

Subsequent disposable local HTTP probes used synthetic data, the real Django and
SQLite application, and four Waitress request threads without contacting a cluster:

| Probe | Observed result |
| --- | --- |
| 180 sequential create/prepare operations with concurrent context/job reads | 848 health checks; maximum latency 21 ms |
| Eight writers preparing 280 jobs | 517 temporary publication conflicts recovered by client retries; maximum health latency 223 ms |
| Eight writers preparing 640 jobs, with a 64 MiB fixture metadata allowance | 1,123 temporary publication conflicts recovered; maximum health latency 217 ms |
| 641 retained jobs and 1,001 seeded terminal upload receipts, followed by 128 unique-name preparations | Receipt pruning retained exactly 1,000 rows; 293 temporary publication conflicts recovered; maximum health latency 263 ms |
| Four clients stopped reading 4 MiB/24 MiB artifact replies; 16 clients disconnected before receiving preparation replies | Stable-key retries recovered 16 distinct jobs; maximum health latency 3 ms |
| Actual stdio MCP server, eight client lanes, 100 jobs and 100 stable-key preparation replays through four-thread Waitress | 100 distinct executions and verified outputs; exactly 100 synthetic CPU-core-seconds settled, zero reserved; all 948 portal/health probes succeeded, maximum latency 191 ms |

Cold request-handler imports completed concurrently in the receipt-pruning probe.
The MCP fixture completed in 63.5 seconds while both full Python test suites
were running. It covers the actual MCP protocol process rather than invoking
its facade directly; it still uses a synthetic backend and proves no native
Slurm or SSH behavior. Reproduce it with `tools/local_smoke.py --mode budgeted --mcp-jobs 100`.
These probes did not reproduce the original stall; they do not establish a fix.
The [responsiveness roadmap item](roadmap.md) remains open pending a captured
recurrence and a regression test for its actual cause. Server thread counts and
database timeouts remain unchanged.

## Portal and agent interface

The human portal configures clusters, discovery, execution recipes, projects,
budget/review policy and project keys. Keys are shown once and stored as hashes.
Each key belongs to one project and has explicit scopes; tighter key policy may
require approval even where the project permits automatic work. Multiple keys
share the same project allowance and lifetime submission count.

`compute-relay init-mcp` creates project-local stdio MCP configuration for Codex
or Claude Code. It defaults to the local broker URL and prompts privately for the
project API key. The local facade holds that project key; cluster keys and MFA
remain in the connector. Source staging and artifact downloads use bounded APIs
and verify checksums. See [client setup](local-client-setup.md).

The package exposes project-local stdio MCP clients. Remote HTTP MCP and OAuth
are not provided; see the [roadmap](roadmap.md).

An initialized workspace can also use the public [Python SDK](python-sdk.md) to
prepare, request, monitor and export jobs in a bounded local script. It reuses
the MCP adapter's client and operations with the same project credential and
broker authorization. Intermediate responses can stay outside model context;
HTTP requests, human review and scheduler waits still apply.

The portal groups project policy, allowance, resource ceilings and access;
cluster and environment grants can be changed together. New connections use
Slurm without a backend selector, and SSH overrides are under advanced settings.
Editable cluster walltime and SSH lifetime use minutes; saved worker profiles
and API accounting retain integer seconds. Key confirmation follows project
policy or always requires review.

Dashboard and project jobs share 50-row pages with All, Unresolved and Awaiting
review filters. Live updates keep the selected page/filter and unsent edits;
failed review submissions retain the matching comment. Separate pending-review
notifications show the oldest 20 requests and their total count. Job pages offer
safe cancellation, collection retry and verified attachment downloads for declared
results, plus workspace-export guidance. Cancellation and collection keep the
original attempt and accounting; neither admits compute again. Cancellation
needs no additional retry-receipt capacity. Disabling new access does not block
recovery of existing results through a current owned connector.

Allowance uses CPU core-hours/GPU device-hours with exact seconds available.
Project capacity separately shows stored/reserved bytes, pending transfers,
objects, outstanding work, burst capacity, active retry receipts, module definitions/runs and
archived identities, with installation limits where applicable. These read-only
logical counters neither scan the disk nor promise physical free space. Optional
environment-build pages show whether a ready environment is actually permitted;
storage settings remain advisory planning estimates. See the
[portal guide](management.md) and remaining
[human/site acceptance work](roadmap.md).

The alpha supports fresh setup after incompatible stored-state changes, rather
than historical schema upgrades. See [alpha updates](deployment.md#alpha-updates-and-fresh-setup).

## Preparation, approval, and accounting

A job revision binds the exact source files, generated batch script, environment
wrapper, connection revision and maximum resource allocation into a canonical
manifest. Preparation does not execute the uploaded code. Approval binds one
execution request to that exact manifest. Rejection can carry a comment to the
agent; a changed script or resource request requires a new revision and decision.

Automatic execution still checks project policy, current key authority, active
job limits and the lifetime total-job limit. CPU-core-seconds and GPU-device-
seconds are separate balances. Pending approval can hold its declared maximum
charge; admission converts that hold instead of counting it twice. A durable
attempt consumes one lifetime slot even when it later fails or is cancelled.

Dispatch rechecks authorization and obtains online confirmation. The worker
journals intent before remote submission, and the remote helper journals the
attempt before `sbatch`. Lost replies remain uncertain and reconcile the same
attempt. Missing queue rows do not prove non-submission. Definitive scheduler
accounting settles a completed allocation; cancellation acknowledgement alone
is not a refund. Ledger transitions use SQLite immediate transactions and
uniqueness/generation fences rather than an in-memory quota counter.

Slurm elapsed time is metered in scheduler whole seconds, with at least one
second for an allocation that received CPUs or GPUs. Reservation uses the
reviewed effective allocation multiplied by requested walltime. No separate
charge duration or scheduler/cleanup interval is added.
This is broker accounting, not a physical nanosecond ceiling or a restriction on
all activity under the remote account.

Cancellation has an explicit result. Only an acknowledged native cancellation
or an identity-verified terminal observation resolves its pending intent. A
timeout, malformed result or empty queue response retains the intent and
reservation, with bounded retries and observation. A live identity mismatch
surfaces `CANCEL_IDENTITY_MISMATCH`; repeated cancellation is withheld until an
explicit new request, while observation can still establish completion. Even an
acknowledged cancellation waits for definitive accounting before settlement.

Eligible work is filtered before the controller's bounded candidate batch.
Cancellation, uncertain-submission reconciliation and existing-job observation
precede new submissions. Paused projects, disabled connections and unsupported
extensions therefore cannot fill the first batch with undispatched work and
hide later management of running jobs.

Cluster removal normally waits for final accounting and output collection. The
local owner can explicitly **Remove anyway** after acknowledging the displayed
unfinished counts and budget holds. This archives only the configuration: it
does not synthesize cancellation, settle unknown usage, release reservations or
reset job limits. Exclusive personal connectors are revoked; shared connectors
can continue accounting for existing jobs. Retained unresolved job pages show
that their cluster configuration was removed and their status may be stale.

## Local storage and metadata bounds

Object storage and durable metadata have separate admission accounting. Current
object defaults are 64 GiB per project, 256 GiB per installation and 10,000 objects
per project, including pending capacity in quota checks. Ordinary byte uploads
are bounded to 512 KiB each; the resumable data path supports 8 GiB per file and
64 GiB per job in either direction. Slurm and simulated jobs publish outputs
through this same resumable path; final reports carry status and accounting,
not file bytes. Eight pending upload reservations per project are allowed.
See [files and live output](data-transfer.md) for limits, recovery and
incremental log semantics.

Each worker serializes its output-write requests through one FIFO asynchronous
lock, preventing its own collectors from starving one another at the object
store's writer lock. Remote chunk reads, status checks and lease renewals remain
concurrent. Publication retries only an explicit `409 UPLOAD_BUSY`, reusing the
unchanged request and already fetched remote chunk. Each request has at most
eight attempts, with jittered delays capped at two seconds and at most 7.1
seconds of added sleep. Cancellation or lost authority stops collection. Quota,
integrity and other errors retain the separate bounded collection-recovery
path without resubmitting compute or charging it again. Sustained contention
can still exhaust automatic recovery and require an explicit output retry.
If a retry can reuse verified bytes already published by another transfer, it
releases its own redundant partial reservation immediately. Other transfers'
reservations and independent input-upload authority remain unchanged.

### Burst, outstanding-work and retained-data limits

Job creation and execution requests each have a durable token bucket at project
and installation scope. A new project can create 500 jobs in a burst; its bucket
refills at 60 requests per minute. Installation buckets allow 1,000 initially
and refill at 300 per minute. Preparation does not spend execution tokens.
Multiple keys share both bounds, and exact idempotent retries spend neither a
second token nor another outstanding slot. Rejected transactions roll back
their rate charges, reservations and identities together.

Outstanding work defaults to 1,000 slots per project and 5,000 per installation.
Unexecuted draft/prepared packages, pending reviews and unresolved attempts
consume slots. A first execution replaces its draft slot; another execution
needs another slot. Completion frees its slot after accounting and collection
close out; cancellation or rejection of unused work frees its slot without
deleting durable history. Explicit output recovery and worker reports remain
available at capacity. Reopening output recovery can temporarily exceed the
ceiling, while further new work stays blocked. Workflows wait for capacity and
resume the same node instead of treating throttling as a policy rejection.

`allowance.admission` and the portal show project and installation headroom.
`429 RATE_LIMITED` includes retry seconds and a `Retry-After` header;
`409 BACKLOG_LIMIT` identifies the full scope but cannot predict when a job will
finish. Spending allowance, lifetime submission policy, concurrent execution,
traffic rates, outstanding work and retained storage are separate bounds.
Operators can adjust `BROKER_PROJECT_ADMISSION_BURST`,
`BROKER_INSTALLATION_ADMISSION_BURST`, `BROKER_PROJECT_ADMISSION_PER_MINUTE`,
`BROKER_INSTALLATION_ADMISSION_PER_MINUTE`,
`BROKER_MAX_PROJECT_OUTSTANDING_JOBS` and
`BROKER_MAX_INSTALLATION_OUTSTANDING_JOBS`; all must be positive.

Logical metadata charges default to 32 MiB per project and 256 MiB across the
installation. Job request/manifests, idempotency responses and extension
definition/run documents consume these charges even when their source blobs
deduplicate. Additional record limits apply:

| Metadata family | Per project | Per installation |
| --- | ---: | ---: |
| Active idempotency responses | 10,000 | 100,000 |
| Combined extension definitions/runs | 1000 | 10,000 |
| Archived job identities | 10,000 | 100,000 |
| Rolling audit events | 2000 | 20,000 |

Audit details are bounded and oversized details are replaced with a digest.
The controller retains at most 16 report receipts per attempt; pruned older
generations fail the ordinary stale-lease fence. Attempts, accounting entries
and execution identities remain durable. Metadata exhaustion rejects new
metadata admissions with `METADATA_QUOTA`; it does not block valid worker
accounting reports. Errors identify the exhausted scope, resource, usage, limit
and remaining headroom. Retained jobs have a logical byte charge rather than a
second lifetime job-count cap. See [maintenance](deployment.md#inspect-and-compact-local-metadata).
Compacting old idempotency responses frees active key slots. Their small
identity tombstones still consume logical byte quota and prevent an old key
from executing again.

These are logical JSON/record charges and bounded retention rules, not a byte
ceiling on SQLite, its indexes/WAL, journals, backups or filesystem allocation.
Keep physical free space and inodes available. Advisory remote storage targets
also cannot prevent unrelated users or jobs from filling a shared filesystem.

Content hashes identify bytes, not permission to reuse them. A job input is
sealed to an independently uploaded object or an exact artifact the current
credential can read, including access to its producer connection. Reading a
package, preparing/revising/executing it, and reusing workflow/environment
definitions recheck that source authority. Deduplication never creates an upload
grant for generated or reported output bytes; an explicit byte upload does.
See [security](security.md#input-provenance-and-object-publication).

## Cluster and software support

The native backend supports Slurm over SSH, independent accounts and keys per
cluster, and human MFA at SSH login. A one-shot bounded Python helper invokes
fixed scheduler operations; it exposes no arbitrary shell RPC. The worker
reuses its authenticated SSH master and does not fall back to a new unattended
login when that master disappears.

Status reads use one interface for one through 32 jobs. The controller groups
consecutive due observations on the same connection, within an 8 MiB additional
payload bound; it never skips older submission, cancellation or recovery work
to fill a group. A single query reads the live jobs, followed by accounting for
jobs absent from the queue. Every item retains its own verified journal binding,
generation-fenced lease, heartbeat, accounting and output-recovery state.
Invalid or uncertain status data never establishes completion or releases funds.

Single-job and grouped work share the same package verification, durable report
replay and per-job finalization. Completed items stop renewing and release their
local in-progress slot independently. At most four remote operations run by
default; `COMPUTE_RELAY_MAX_REMOTE_OPERATIONS` permits one through eight. Status
grouping is always enabled and needs no experimental switches or persistent
remote Python process. Discovery and optional modules use the same SSH transport.

The fixed remote source is assembled from reviewed standard-library components
into separate execution/management and discovery bundles. New prepared jobs bind
`helper_protocol=slurm-jobs/v1` and the exact execution-bundle SHA-256. Management
uses current compatible code plus the original package digest and exact remote
journal/native identity. It never runs helper source obtained from an old
manifest. New submissions still reject a changed execution hash. Incompatible
alpha updates require [fresh setup](deployment.md#alpha-updates-and-fresh-setup)
after outstanding work is reconciled.

Discovery separates configured capabilities, authorization and observed idle
resources. Software/module catalogs are login-node observations, with provenance
and partial/truncated flags. A selected module or interpreter is not thereby
verified on compute nodes. See [module discovery](module-discovery.md) and
[cluster checks](cluster-checks.md).

Single-node jobs with bounded resources are supported. Arrays, automatic requeue,
arbitrary scheduler directives, other schedulers and persistent interactive
runtimes remain unsupported. Explicit retries create separate attempts and
reservations. The fake backend simulates outcomes without executing uploaded
code in the connector.

## Acceptance evidence and remaining reliability work

The 28 October 2026 acceptance on a shared four-node Slurm cluster verified
1,144 unique native jobs, including four deliberate cancellations, with matching
native/controller CPU accounting and no duplicates. Requests used one CPU,
1 GiB and a one-minute limit; the site allocated two logical CPUs. The run used
no GPUs or interactive partition and took approximately 82 minutes. Lost submit
responses and SSH interruptions preserved durable job identity. A separate
200-job test through the installed MCP stdio server passed 2,441 tool calls,
200 idempotent preparation replays and result-artifact verification.

The equal-sized comparison used 156 jobs per mode. Bounded status grouping
reduced scheduler-status queries by 77.7%; keeping Python helpers alive reduced
source transfers without establishing a clear throughput advantage. The chosen
architecture keeps one SSH transport and bounded status grouping. Detailed
experiment material is retained in Git history rather than maintained as a
second implementation or benchmark framework.

After consolidation, a disposable local Slurm acceptance submitted two jobs
under a different reviewed execution-bundle hash, deliberately lost one submit
reply, and verified reconciliation, cancellation, status reads, collection and
final native accounting through the current helper. Recovery retained the same
identities and made exactly two native submission calls. This checks helper
upgrade/recovery behavior; it does not replace real-site load acceptance.

A separate local contention probe used the real four-thread HTTP server,
on-disk SQLite and eight worker lanes to collect 100 unique 64 KiB outputs from
already-settled synthetic attempts. Short contention retries alone completed
83 on the first pass and left four partial reservations. With the worker's
FIFO publication lock, the production execution path collected all 100 in
14.839 seconds with exactly 200 output requests, no publication conflicts or
pending reservations, and unchanged 100 settlements / 200 CPU-core-seconds.
All 447 health requests succeeded; maximum latency was 86 ms. This measures
local publication recovery, not SSH, Slurm or large-file throughput.

Four successful jobs required explicit output recovery after automatic retries
were exhausted by publication contention. One portal stall during preparation
remains unexplained. Object capacity was raised from 1,000 to 10,000 after the
former limit was reached at only 506 KiB. The test also reached the normal
32 MiB logical-metadata ceiling after 788 admissions; its temporary 64 MiB test
override was removed afterward. Those retained-data bounds remain independent
of the burst and outstanding-work controls described above. The [roadmap](roadmap.md)
tracks remaining real-site acceptance, portal-stall diagnosis and retention work.

## Bundled modules

The normal launcher enables storage, environments, and workflows. Their code,
tables, scopes and lifecycle hooks remain separate from core admission:

- Storage provides advisory byte/inode planning and configured storage targets.
  It does not enforce operating-system quotas or automatically move files.
- Environment builds run through ordinary charged jobs and publish only after
  successful finalization. Supported build recipes are narrower than every
  possible Conda, Poetry or module setup; inspect [environment builds](environment-builds.md).
- Workflows materialize finite artifact-dependent jobs through the same approval,
  budget and submission limits; see [workflows](workflows.md).

Direct cluster-to-client artifact transfer, packed environment migration, native
arrays and alternate schedulers are unsupported; see the [roadmap](roadmap.md).
The local process/account boundary and same-UID cluster limits are described in [security](security.md).
