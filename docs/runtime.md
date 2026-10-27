# Implemented local runtime

Compute Relay is a single-user Python package. The normal command is
`compute-relay` (equivalently `compute-relay start`). It runs a Django portal
through Waitress on loopback, maintains a durable SQLite ledger and local object
store, and supervises saved SSH connectors in the foreground. It requires
Python 3.12/3.13 and system OpenSSH for real cluster access. Docker, PostgreSQL,
LDAP and a separately managed control service are unnecessary.

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
development checks. `requirements.lock` constrains reproducible source/fixture
installs; a normal `pip install compute-relay` needs no extras.

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

The package exposes neither an HTTP MCP endpoint nor an OAuth authorization
server. The prior multi-user identity/database runtime is retained only on
`codex/archive-multi-user-server` at `520effc`.

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
object defaults are 64 GiB per project, 256 GiB per installation and 1000 objects
per project, including pending capacity in quota checks. Ordinary byte uploads
are bounded to 512 KiB each; the resumable data path supports 8 GiB per file and
64 GiB per job in either direction. The legacy inline terminal report retains
its 10 MiB artifact bound. Eight pending upload reservations per project are
allowed. See [files and live output](data-transfer.md) for limits, recovery and
incremental log semantics.

Logical metadata charges default to 32 MiB per project and 256 MiB across the
installation. Job request/manifests, idempotency responses and extension
definition/run documents consume these charges even when their source blobs
deduplicate. Additional record limits apply:

| Metadata family | Per project | Per installation |
| --- | ---: | ---: |
| Charged job records | 1000 | 10,000 |
| Idempotency identities, including compacted ones | 10,000 | 100,000 |
| Combined extension definitions/runs | 1000 | 10,000 |
| Archived job identities | 10,000 | 100,000 |
| Rolling audit events | 2000 | 20,000 |

Audit details are bounded and oversized details are replaced with a digest.
The controller retains at most 16 report receipts per attempt; pruned older
generations fail the ordinary stale-lease fence. Attempts, accounting entries
and execution identities remain durable. Metadata exhaustion rejects new
metadata admissions with `METADATA_QUOTA`; it does not block valid worker
accounting reports. See [maintenance](deployment.md#inspect-and-compact-local-metadata).

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

The fixed remote source is assembled from reviewed standard-library components
into separate execution/management and discovery bundles. New prepared jobs bind
`helper_protocol=slurm-jobs/v1` and the exact execution-bundle SHA-256. Management
uses current compatible code plus the original package digest and exact remote
journal/native identity. It never runs helper source obtained from an old
manifest. New submissions still reject a changed execution hash. Use the offline
[upgrade check](deployment.md#check-an-upgrade-before-starting-it) before starting
an updated installation with outstanding work.

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
arrays and alternate schedulers remain planned. The local process/account
boundary and same-UID cluster limits are described in [security](security.md).
