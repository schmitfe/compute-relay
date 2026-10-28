# Local asynchronous Slurm connectors

The normal `compute-relay` launcher starts personal SSH connectors using the
asynchronous worker, HTTP client, Slurm adapter and OpenSSH transport. Saved
connectors belong to the local installation. SSH login and MFA are serialized
in the terminal; bounded worker operations then await network/process I/O
without a thread-pool wrapper or automatic transport retry.

## Core and bundled modules

The worker handles submission, reconciliation, observation, cancellation,
bounded output collection and discovery. It shares exact package verification,
batch rendering, native identity fences and accounting parsers with the core
backend. The connector has no direct controller database access.

Transport-independent rules live in `domain/lifecycle.py`: typed operations and
outcomes, bounded package decoding, submit-versus-reconcile decisions, native
identity checks, cancellation mapping and durable-report replay. The worker
and the controller validate the same operation/outcome combinations. Their I/O
remains separate, and controller accounting transactions remain explicit.

New submissions verify the exact installed execution-helper hash. Observation,
cancellation, reconciliation and collection validate the supported management
protocol and original immutable package instead. Remote management additionally
requires the original journal manifest digest, embedded manifest, exact native
identity and derived attempt directory. Discovery has its own deterministic
stdlib bundle and cannot change the execution identity with a catalog-only edit.
Components declare their own imports; the fixed linker checks provider exports
and dependency cycles before assembling a deterministic program. The worker
selects implementations through the trusted backend registry.

The launcher passes its selected bundled module names through connector startup.
The worker advertises only those modules and imports implementations only from
the trusted registry. Current async module operations include:

- Storage maintenance validates the leased target/root/context and awaits its
  fixed remote capacity probe and authenticated report.
- Environment finalization awaits the fixed metadata helper after a terminal
  build observation. The connector never runs the built interpreter or installer.
- Workflow terminal hooks preserve their metadata contract; concrete workflow
  nodes still use normal core jobs and accounting.

The async extension entrypoints are `execute_extension_work` and
`worker_after_observation`, with module-owned `execute` and
`after_observation` implementations. Disabled or unsupported module work
fails closed. Hook failures are redacted and must not prevent valid core
accounting or collection from reaching the controller. Module enablement is not
permission to build, probe or submit; current project scopes/policy and lease
identity checks still apply.

The launcher, developer tools and fixtures use the same asynchronous worker,
Slurm adapter and OpenSSH transport. There is no synchronous execution mode.
This release supports Slurm. Other schedulers are unsupported; see the
[roadmap](roadmap.md) before adding one.

Python integrations use `Worker` from `worker.runner`, `SlurmBackend` from
`backends.slurm`, and `OpenSSHTransport` from `transports.openssh`, and await
their execution methods. The former `async_*` modules and `Async*` class names
have been removed. `python -m compute_relay.worker` starts this same worker
without a mode switch; its protected configuration includes `owner_id` (or
the operator supplies `BROKER_WORKER_OWNER_ID`). Saved personal connectors use
their existing owner identity and state.

## Bounds and durable recovery

Each personal connector runs up to four independent job operations concurrently
over its existing authenticated OpenSSH master. This uses multiplexed command
sessions, not additional interactive logins. Set
`COMPUTE_RELAY_MAX_REMOTE_OPERATIONS` before startup to an integer from 1 through
8 if the site's SSH session limits or measured throughput require a different
bound. The normal local launcher also limits all its connectors together to
16 operations. Separate launcher processes have separate process bounds.

The number of remote commands is independent of the number of admitted Slurm
jobs: a project can allow up to 1,000 active jobs without opening 1,000 SSH
sessions. Existing project settings are preserved. Slurm and account limits
still determine which submitted jobs actually run. Short jobs may still cost
more to stage and schedule than to execute locally; this release uses individual
submissions and observations rather than batched scheduler queries.

Finite concurrency/pending limits bound work inside the worker.
These process-level controls do not replace database reservations, active/lifetime
caps, generation-fenced leases or journal locks. SQLite remains the admission
and accounting authority across simultaneous HTTP clients and connectors.

The controller filters ineligible undispatched work before its bounded candidate
slice. The oldest due time wins across both authorized submissions and existing
job management; cancellation and uncertain-submission reconciliation break
equal-time ties. A new observation cannot jump ahead of an older submission,
and new submissions cannot jump ahead of an overdue observation. Repeated slow
cancellation failures back off instead of monopolizing the worker. Queue length
and operation duration still determine the actual wait; there is no fixed
wall-clock latency guarantee. A paused project or
disabled/unsupported submission cannot occupy that slice indefinitely and hide
a running attempt, including another project's work on the same worker.

The first check after submission becomes due after two seconds. Subsequent
queued/running observations back off to reduce scheduler traffic: their ceiling
is five seconds for small populations, increasing to at most 60 seconds as the
worker's unsettled attempt count grows. Cancellation, reconciliation and pending
settlement retain separate retry rules. Dispatch does not sleep after each
successful command; an idle lane waits two seconds before polling again.

Lease fetching and local profile changes are serialized. In-progress attempt
IDs are excluded from new polls, and each attempt retains its own durable
journal lock. Discovery and extension maintenance wait for exclusive access;
requested configuration changes stop new leases until existing operations drain.
Folder browsing and environment inspection can share the bounded pool with job
operations because they do not change the selected connection or baseline.

The worker persists submission intent before contacting Slurm. A lost reply or
cancelled SSH operation reconciles that same attempt; it never retries `sbatch`
blindly. Even a new `submit` lease becomes reconciliation when a prior durable
intent exists. Changing helper code cannot convert that intent into permission
for another submission.

Core leases last 120 seconds and renew every 20 seconds using the exact attempt,
lease ID and generation. Each generation has a fixed 40-minute overall deadline;
renewal cannot move that deadline. A replaced generation remains fenced. An
expired but unsuperseded lease can renew only within its original deadline.
Failed renewal stops further progress on that operation and cancels its owned
I/O, while durable evidence remains available for recovery.

A terminal observation is written to the local report journal **before** module
finalization and output collection. A complete report is stored before delivery,
and ready reports are replayed before fresh work is polled. Lost report replies
reuse their receipt rather than repeating submission. Interrupted collection can
resume from the saved terminal evidence; finalization/collection failure does
not invalidate definitive scheduler accounting. These reads may repeat, but
they do not run the uploaded program in the connector.

When a replacement lease supersedes a report, current management authority may
reuse terminal evidence only for the same attempt, manifest and native identity.
Changed extension context drops the cached extension metadata while preserving
eligible core accounting; it cannot grant new extension authority. The
controller retains the latest 16 receipts per attempt. Pruned old generations
are rejected as stale, while attempts and ledger entries remain durable.

Cancellation responses are explicit: `cancel_requested`, `already_terminal`,
`identity_mismatch` or `unknown`. A timeout, malformed reply or missing queue row
does not acknowledge success. Unknown responses keep cancellation pending and
trigger bounded observation/retry with backoff. A terminal response must be
confirmed by an identity-bound terminal observation. Identity mismatch is
surfaced and suppresses repeated cancellation until an explicit new request;
observation can still resolve completion. Acknowledged cancellation alone does
not settle or refund the compute reservation.

Small package-validation and journal read/write/fsync operations remain local
synchronous work. They should use private local storage. Async I/O does not
promise that an arbitrary slow network filesystem or large serialization task
cannot delay a process.

## SSH, HTTP and shutdown

The transport constructs fixed OpenSSH arguments from the reviewed connection:
pinned hosts, explicit identity/session, no agent/X11 forwarding and no arbitrary
agent-controlled options. The connector's authenticated master is required;
its failing proxy fallback prevents a dead master from triggering a fresh
unattended login. OpenSSH prompts happen only in the local startup terminal.

Async SSH streams input with backpressure and drains both outputs with finite
byte and whole-operation time limits. Timeout, overflow or cancellation stops
the operation's local process group and reaps its child. Cleanup retains ownership
of a child created concurrently with cancellation. Kernel uninterruptible waits
remain outside Python's ability to force immediate cleanup.

HTTP verifies its configured origin, rejects redirects/environment proxies and
compressed bodies, and enforces streamed byte and total time limits. Mutating
requests are not automatically replayed after timeout.

## Outage recovery

Personal connectors share exponential retry gates across their concurrent
operations. After a controller or remote-helper failure, retries wait with jitter
and a 60-second ceiling; only one operation tests recovery after each cooldown.
Older in-flight successes cannot clear a newer failure. HTTP retries happen at
the worker's durable polling/report boundary, not by blindly resending arbitrary
mutations. Controller 401/403 responses stop the connector instead of retrying
its credential. Helper calls continue to require the owned master and cannot
fall back to another SSH login themselves.

If the authenticated master disappears, all operation lanes drain first. Their
submission intents and pending reports remain on disk. The connector retires its
old master and checks TCP reachability without sending SSH credentials. Checks
wait approximately 2, 4, 8, 16, 32 and then up to 60 seconds, with jitter and at
most twelve probes per outage. A still-live or replaced socket is not removed;
only the unchanged, owned socket with a refused local connection can be cleaned
up after an abrupt master death.

Once the network is reachable, recovery makes one noninteractive, pinned-host,
explicit-key authentication attempt. Password and keyboard-interactive/MFA
authentication, agents and prompts are disabled. Any failed handshake or
authentication opens the circuit until an explicit connector restart. Successful
but flapping sessions allow at most three automatic reconnect attempts per
connector launch, separated by at least 60 seconds. Recovery never extends the
original session deadline.

An encrypted key that needs its passphrase again, an MFA-only login, a changed
host key or rejected credentials therefore needs human login through the usual
local start action. No passphrase, password or MFA answer is retained or replayed.
An unencrypted authorized key can reconnect automatically. On success the same
worker identity and journals resume: uncertain submissions reconcile, confirmed
Slurm IDs remain the same, and existing budget reservations are preserved.
Interrupted work may wait for its previous lease to expire (normally up to
120 seconds) before another generation can reconcile it; recovery does not
bypass that fence.

Ctrl-C cancels and drains all owned operation lanes before closing their SSH
master and the local portal. A fatal authentication failure also drains every
lane before closing the session. Slurm allocations
already accepted continue remotely. Restarting the same installation reconnects
and resumes durable observation/reconciliation rather than creating replacement
attempts. Individual session lifetime remains finite and another login may be
required.

## Verification

Default worker/transport tests use controlled local processes, stub schedulers
and synthetic credentials. They cover bounds, cancellation, journal recovery,
cross-version helper management, current module capabilities, slow collection,
renewal failure, superseded generations, lost final reports and preservation of
core settlement. Lifecycle cases exercise the single maintained worker implementation.
Scaling regressions cover two continuously running jobs plus eight pending
submissions, 200 admitted attempts with due observations, a 200-job worker queue
bounded to four concurrent operations, duplicate attempt execution and parallel
shutdown. The personal connector Slurm fixture additionally checks that eight
distinct scheduler IDs are assigned before the first jobs finish, even though
its two-CPU scheduler cannot run all eight jobs at once. It then kills the local
SSH master abruptly and verifies automatic reconnection, unchanged native job
IDs, final accounting and clean shutdown.
A clean installed-wheel check starts the actual loopback portal
and verifies shutdown/restart without connecting SSH. Optional disposable Slurm
acceptance provides separate native scheduler evidence; neither establishes
same-account isolation or institutional MFA policy.
