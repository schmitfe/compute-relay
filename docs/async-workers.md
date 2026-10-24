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
identity checks, cancellation mapping and durable-report replay. Both runners
and the controller validate the same operation/outcome combinations. Their I/O
remains separate, and controller accounting transactions remain explicit.

New submissions verify the exact installed execution-helper hash. Observation,
cancellation, reconciliation and collection validate the supported management
protocol and original immutable package instead. Remote management additionally
requires the original journal manifest digest, embedded manifest, exact native
identity and derived attempt directory. Discovery has its own deterministic
stdlib bundle and cannot change the execution identity with a catalog-only edit.
Components declare their own imports; the fixed linker checks provider exports
and dependency cycles before assembling a deterministic program. Both worker
adapters select implementations through the same trusted backend registry.

The launcher passes its selected bundled module names through connector startup.
The worker advertises only those modules and imports implementations only from
the trusted registry. Current async module operations include:

- Storage maintenance validates the leased target/root/context and awaits its
  fixed remote capacity probe and authenticated report.
- Environment finalization awaits the fixed metadata helper after a terminal
  build observation. The connector never runs the built interpreter or installer.
- Workflow terminal hooks preserve their metadata contract; concrete workflow
  nodes still use normal core jobs and accounting.

The async extension entrypoints are `execute_extension_work_async` and
`worker_after_observation_async`, with module-owned `execute_async` and
`after_observation_async` implementations. Disabled or unsupported module work
fails closed. Hook failures are redacted and must not prevent valid core
accounting or collection from reaching the controller. Module enablement is not
permission to build, probe or submit; current project scopes/policy and lease
identity checks still apply.

The synchronous runner remains a lower-level developer/test implementation.
It is not required to run the local package's bundled capabilities. This
release supports Slurm; alternate schedulers remain planned.

## Bounds and durable recovery

Owner lanes and finite concurrency/pending limits bound work inside the worker.
These process-level controls do not replace database reservations, active/lifetime
caps, generation-fenced leases or journal locks. SQLite remains the admission
and accounting authority across simultaneous HTTP clients and connectors.

The controller filters ineligible undispatched work before its bounded candidate
slice. It prioritizes pending cancellation, uncertain-submission reconciliation
and existing-job observation ahead of new submissions. Within management work,
the oldest due time wins; cancellation and reconciliation break equal-time ties.
Repeated slow cancellation failures therefore cannot monopolize the worker.
A paused project or
disabled/unsupported submission cannot occupy that slice indefinitely and hide
a running attempt, including another project's work on the same worker.

The worker persists submission intent before contacting Slurm. A lost reply or
cancelled SSH operation reconciles that same attempt; it never retries `sbatch`
blindly. Even a new `submit` lease becomes reconciliation when a prior durable
intent exists. Changing helper code cannot convert that intent into permission
for another submission.

Core leases last 120 seconds and renew every 20 seconds using the exact attempt,
lease ID and generation. Each generation has a fixed 40-minute overall deadline;
renewal cannot move that deadline. A replaced generation remains fenced. An
expired but unsuperseded lease can renew only within its original deadline.
Failed renewal stops further progress on that operation; the async path cancels
its owned I/O, while durable evidence remains available for recovery. The
synchronous adapter's heartbeat thread performs HTTP only, never SSH.

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

Both transports construct fixed OpenSSH arguments from the reviewed connection:
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

Ctrl-C closes managed connector sessions and the local portal. Slurm allocations
already accepted continue remotely. Restarting the same installation reconnects
and resumes durable observation/reconciliation rather than creating replacement
attempts. Individual session lifetime remains finite and another login may be
required.

## Verification

Default worker/transport tests use controlled local processes, stub schedulers
and synthetic credentials. They cover bounds, cancellation, journal recovery,
cross-version helper management, current module capabilities, slow collection,
renewal failure, superseded generations, lost final reports and preservation of
core settlement. Shared lifecycle cases run against both worker variants.
A clean installed-wheel check starts the actual loopback portal
and verifies shutdown/restart without connecting SSH. Optional disposable Slurm
acceptance provides separate native scheduler evidence; neither establishes
same-account isolation or institutional MFA policy.
