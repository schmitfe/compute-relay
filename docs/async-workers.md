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

The worker persists submission intent before contacting Slurm. A lost reply or
cancelled SSH operation reconciles that same attempt; it never retries `sbatch`
blindly. Saved native receipts are replayed after failed reports. Terminal
collection/module failure does not invalidate definitive scheduler accounting.

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
current module capabilities, failed finalization/reporting and preservation of
core settlement. A clean installed-wheel check starts the actual loopback portal
and verifies shutdown/restart without connecting SSH. Optional disposable Slurm
acceptance provides separate native scheduler evidence; neither establishes
same-account isolation or institutional MFA policy.
