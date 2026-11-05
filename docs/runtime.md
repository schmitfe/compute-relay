# Runtime and limits

Compute Relay runs a loopback Django/Waitress portal, durable SQLite ledger,
local object store and personal SSH connectors. Use [installation](deployment.md)
for startup/update instructions, [portal management](management.md) for settings,
and [architecture](architecture.md) for the execution/authority model.

## Startup and persistent state

`compute-relay` supervises the portal and its installation's saved connectors.
Ctrl-C stops them without cancelling Slurm jobs; restart resumes saved work after
any required SSH login. Budgets, jobs, artifacts and connector journals persist.
Incompatible updates use [fresh setup](deployment.md#alpha-updates-and-fresh-setup),
not historical state conversion. Reconcile work and preserve evidence first.

### SSH expiry and reconnecting a stopped connector

Sessions default to eight hours. Expiry closes the SSH master after draining
current operations, without cancelling native jobs. Owner-enabled automatic
reconnection uses eligible noninteractive keys only; failed authentication needs
human action. Passwords, passphrases and MFA are never retained or replayed.
Use **Reconnect** on the cluster page and complete prompts in the application
terminal. [Connector guidance](personal-connectors.md) covers setup and recovery;
never delete a lock or journal to bypass `STATE_LOCKED`.

For unresolved work, `tracking.status` is `recent` within five minutes of contact,
then `stale`, or `not_seen`/`revoked`. Recent contact is not proof of fresh native
observations; saved `auth_state: ready` is not current connectivity. Settled,
collected attempts have `tracking: null`. A `reconnect_connector` next action
requires human attention, not another execution request.

Terminal computation and available accounting are reported before logs,
finalization and output transfer. Collection has an independently recoverable
lease and can proceed with pending accounting; unknown usage keeps its
reservation. One lease per attempt means its next accounting poll can wait for
collection. Slurm success does not establish delivered results.

### Diagnosing an unresponsive portal

The launcher checks identity-bound health every 15 seconds. After three failed
checks it asks its own web child for one all-thread stack dump in private
`web.log`, without restarting or interrupting jobs. Another dump requires health
recovery followed by a new outage. Dumps contain no frame-local values or HTTP
access path.

For manual capture on Linux, read the launcher's PID from private `runtime.json`.
Identify its `-m compute_relay.local_web` child with
`ps -o pid=,args= --ppid LAUNCHER_PID`, then `kill -USR1 WEB_PID`. Use verified
process IDs and inspect `web.log` before restarting. Signal only that web child:
launchers, connectors and older web processes can terminate on `SIGUSR1`. Keep
logs private.

The 28 October 2026 stall lasted at least four minutes with all four request
threads unresponsive and 24 requests queued. No stacks were captured. Later
concurrent HTTP/MCP tests did not reproduce it; its cause remains open in the
[roadmap](roadmap.md).

## Portal and agent interface

The portal owns configuration, grants, approval and one-time project-key
issuance. Agents use project-local [MCP setup](local-client-setup.md) or the
[Python SDK](python-sdk.md); neither receives connector SSH credentials or MFA.
Remote HTTP MCP/OAuth is unsupported.

Source previews preserve stored bytes/hashes: invalid UTF-8 appears as `\xNN`
and invisible/directional/non-printable symbols as Unicode codes. Python,
shell and JSON highlighting covers at most 128 KiB of displayed text; the file
preview ceiling is 1 MiB. Review warnings before approval.

## Preparation, approval, and accounting

Preparation seals exact sources, script, environment, connection revision and
maximum allocation; it executes no workload. Approval permits one request for
that manifest. Changed bytes/resources need a new revision and decision.

Reserve effective CPU/GPU allocation × requested walltime. Pending review holds
convert to attempt reservations once. Final charges use actual allocated
elapsed seconds, including overruns, with at least one second for an allocation
that received resources. No timing/cleanup margin is added. Each admitted
attempt consumes a lifetime slot even if failed/cancelled. This accounts managed
jobs, not every action under the remote UID.

Uncertain submission reconciles the original attempt; absent queue rows and
cancellation acknowledgements cannot release unknown usage. Native identity
mismatch reports `CANCEL_IDENTITY_MISMATCH` and withholds repeated cancellation
until an explicit new request; observation can still resolve it. Existing-work
management precedes new submissions. [Worker recovery](async-workers.md)
describes journals, leases and finite retries.

Configuration edits/re-pairing wait for accounting, collection, cleanup and
leases. **Remove anyway** archives configuration without inventing cancellation,
settlement or released reservations. Unresolved job pages retain stale-status
warnings; remote leftovers can need manual cleanup. See
[cluster removal](management.md#remove-a-cluster-configuration).

Cleanup protects environments/storage roots on the same normalized host, SSH
account and port across connector profiles. Different DNS aliases are not
inferred equivalent. A parent work root permits child-attempt cleanup; a root
inside an attempt protects it. Pending path registration and cleanup fence one
another until canonical paths are known. [Manual cleanup](deployment.md#manual-file-cleanup)
preserves job/accounting identities.

## Local storage and metadata bounds

| Bound | Default |
| --- | --- |
| Object bytes, project / installation | 64 GiB / 256 GiB, including pending reservations |
| Objects / pending upload reservations per project | 10,000 / 8 |
| Inline upload / resumable file / job bundle | 512 KiB / 8 GiB / 64 GiB |
| Source files / exact output paths per job | 100 / 100 |
| Logical metadata, project / installation | 32 MiB / 256 MiB |

Final reports carry status/accounting, not file bytes. Inputs and outputs use
the same verified resumable path; see [transfers](data-transfer.md) for commands
and recovery. Each worker serializes output writes and retries only explicit
`409 UPLOAD_BUSY` with the same request/chunk, at most eight attempts with delays
capped at two seconds. Exhaustion can require explicit collection retry; it
never reruns compute. Cancellation/lost authority stops collection. Reused
verified bytes release only that transfer's redundant reservation.

### Burst, outstanding-work and retained-data limits

| Admission bound | Project | Installation |
| --- | ---: | ---: |
| Initial burst, each of creation/execution | 500 | 1,000 |
| Refill, requests/minute | 60 | 300 |
| Outstanding work | 1,000 | 5,000 |

Keys share these durable limits. Preparation spends no execution token; exact
idempotent retries spend no second token/slot. Rejected transactions roll back
rate charges, reservations and identities together.

Outstanding slots cover drafts/prepared packages, pending reviews and unresolved
attempts. A first execution replaces its draft slot; another needs a new slot.
Accounting/collection closeout or unused-work cancellation/rejection frees the
slot without deleting history. Reports and explicit output recovery remain
available at capacity; reopening collection may exceed the ceiling while new
work stays blocked. Workflows wait and resume the same node after throttling.

`allowance.admission` reports headroom. `429 RATE_LIMITED` supplies retry seconds
and `Retry-After`; `409 BACKLOG_LIMIT` cannot predict job completion. Spending,
lifetime submissions, active execution, rate, outstanding work and retained
storage remain separate. Positive operator overrides are:

- `BROKER_PROJECT_ADMISSION_BURST`, `BROKER_INSTALLATION_ADMISSION_BURST`
- `BROKER_PROJECT_ADMISSION_PER_MINUTE`, `BROKER_INSTALLATION_ADMISSION_PER_MINUTE`
- `BROKER_MAX_PROJECT_OUTSTANDING_JOBS`, `BROKER_MAX_INSTALLATION_OUTSTANDING_JOBS`

| Retained metadata family | Per project | Per installation |
| --- | ---: | ---: |
| Active idempotency responses | 10,000 | 100,000 |
| Extension definitions/runs | 1,000 | 10,000 |
| Archived job identities | 10,000 | 100,000 |
| Rolling audit events | 2,000 | 20,000 |

Request/manifests, responses and module documents consume logical metadata even
when blobs deduplicate. Audit details are bounded; oversized details become a
digest. At most 16 worker receipts remain per attempt; pruned generations fail
the stale-lease fence. Attempts, accounting and execution identities stay durable.
`METADATA_QUOTA` blocks new metadata admissions, not valid accounting reports;
quota errors report scope, usage, limit and headroom. Retained jobs have byte
charges rather than another lifetime count cap. [Compaction](deployment.md#inspect-and-compact-local-metadata)
frees active idempotency slots; identity tombstones still consume bytes and
prevent old keys executing again.

Logical charges do not cap SQLite indexes/WAL, journals, backups or filesystem
allocation. Keep physical bytes/inodes available. Advisory remote targets cannot
prevent unrelated writes filling a shared filesystem. Deduplicated hashes grant
no input authority; see [provenance rules](security.md#input-provenance-and-object-publication).

## Cluster and software support

Single-node Slurm uses one bounded SSH transport over the authenticated master,
with fixed reviewed helpers and no arbitrary shell RPC. One status interface
accepts 1–32 due jobs on one connection within an additional 8 MiB payload bound;
older submission/cancellation/recovery work is not skipped to fill a group.
Each job retains its own verification, journal, lease and finalization.

Accounting requires `sacct --json`; supported allocation formats include Slurm
22.05 and 23.11. There is no unquoted-text fallback. Structured errors, missing
identity/accounting or unsafe text remain unresolved with the reservation held;
warnings alone do not invalidate complete records. The 2 MiB scheduler-response
cap can leave large histories uncertain and require operator review. The
recovery window includes long-running allocations.

New submissions bind the exact installed execution-bundle SHA-256 and
`helper_protocol=slurm-jobs/v1`. Management uses current compatible code with the
original digest/journal/native identity, never source from an old manifest.
[Connector internals](async-workers.md) cover operation bounds and code assembly.
[Job shapes](architecture.md#execution-backends-and-remote-components),
[module discovery](module-discovery.md) and [compute-node checks](cluster-checks.md)
define remaining support limits; login-node visibility is not authorization or
compute-node verification.

## Acceptance evidence and remaining reliability work

On 29 October 2026 the consolidated worker's real-site MCP run completed 101
CPU jobs with verified outputs, stable replays, exact native/controller usage,
zero reserved and normal cleanup. On 5 November, separate-session agmn2
acceptance verified early completion, four outputs (36 MiB), exact accounting,
portal responsiveness, cleanup and owner-confirmed MFA reconnect. Earlier manual
expiry recovery collected 2.44 GB without resubmission; it predates renewal.

Further beta acceptance on 5 November used branch `codex/beta-0.1`:

- Claude Code prepared and retrieved two real agmn2 Monte Carlo jobs through MCP,
  then verified both output hashes with the SDK: four CPU-seconds charged against
  a 900 CPU-second cap, zero reserved and no extra submissions.
- Typed `a6000` and generic `gpu:1` allocations each executed a CUDA kernel and
  returned verified results on an A40. The site intentionally groups this hardware
  under `a6000`; resource labels are not physical-model guarantees. Including one
  failed initial probe, three jobs settled six CPU-seconds/three GPU-seconds, zero reserved.
- Disposable native Slurm passed owner-authorized pinned-wheel venv creation,
  reuse and immutable update (four jobs), a reviewed diamond DAG joining two
  artifact branches (four jobs), and advisory byte/inode probes with no compute
  admission. These module runs are fixture evidence, not agmn2 site certification.

[Automated checks](testing.md) cover publication-hook recovery, native transport
faults and synthetic MCP load. Historical experiments stay in Git history.
Sustained independent-writer/multi-GiB load and additional real-site outage classes
remain [open work](roadmap.md). The original portal stall has not recurred or been
reproduced; investigation is deferred until recurrence.

## Bundled modules

The launcher enables [storage](extensions-storage.md),
[environment builds](environment-builds.md) and [workflows](workflows.md).
[Investigations](investigations.md) is opt-in. Each uses the core ledger and
scopes; storage remains advisory. Developer enablement and disabled-module
behavior are defined in [extensions](extensions.md).
