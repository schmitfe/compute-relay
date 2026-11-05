# Worker execution and recovery

Developer reference for the single asynchronous worker, Slurm adapter and
OpenSSH transport. Operators use [personal connectors](personal-connectors.md).
`domain/lifecycle.py` shares typed outcomes, identity validation and durable-report
rules with control; the worker has no Django/database dependency.

## Core and bundled modules

Python integrations await `Worker` (`worker.runner`), `SlurmBackend`
(`backends.slurm`) and `OpenSSHTransport` (`transports.openssh`).
`python -m compute_relay.worker` uses this same implementation; protected config
supplies `owner_id`, or the operator sets `BROKER_WORKER_OWNER_ID`.

Submission binds the installed execution-helper hash; management validates current
protocol plus original manifest/journal/native identity. Remote bundle construction
is described in [architecture](architecture.md#execution-backends-and-remote-components).
The worker advertises only configured modules. `execute_extension_work` and
`worker_after_observation` call their fixed `execute`/`after_observation` adapters;
disabled or unsupported work fails closed. Hooks cannot undo settlement or execute
payload/installers in the credentialed process. See [extensions](extensions.md).

## Bounds and durable recovery

Default concurrency is four operations per connector over one multiplexed SSH
master; `COMPUTE_RELAY_MAX_REMOTE_OPERATIONS` accepts 1–8. The local launcher caps
its connectors together at 16; independent launchers have independent bounds.
This is not the number of admitted/running Slurm jobs. Status groups contain
1–32 due jobs on one connection; leases, identities and finalization stay separate.

Control filters ineligible work before its bounded candidate slice. Oldest due
time wins; cancellation/reconciliation break ties. Slow cancellation backs off,
so it cannot monopolize work. No fixed latency is promised. The first status is
due after two seconds; queued/running polling backs off from a five-second ceiling
to at most 60 seconds as unsettled population grows. Idle lanes poll after two
seconds; cancellation/reconciliation/settlement use their own retry rules.

Lease fetching/profile changes are serialized, in-progress IDs excluded, and
attempt journals locked. Discovery/maintenance require exclusive access;
configuration changes drain operations first. Browsing and environment inspection
can share the bounded pool because they do not replace the connection baseline.
Process locks do not replace database accounting or generation fences.

Submission intent is durable before SSH. Even a later `submit` lease reconciles
when that intent exists; a lost reply, cancelled I/O or changed helper cannot
justify another `sbatch`. Core leases last 120 seconds, renew every 20 seconds
and have a fixed 40-minute generation deadline. Unsuperseded expiry can renew only
within that deadline. Failed renewal cancels owned I/O and retains evidence.

Every terminal observation in a status batch is journaled before reporting or
reading outputs. Execution/available accounting is delivered first; a separate
single-attempt collection lease handles final logs, module finalization and
outputs. Collection is never part of a status batch. Ready reports replay before
fresh polling and reuse their receipts.

Collection may proceed with unknown final usage, retaining its reservation; the
next accounting poll can wait for this attempt's active collection lease.
Interrupted collection resumes without rerunning code. A replacement generation
can reuse terminal evidence only for identical attempt/manifest/native identity;
changed extension context discards cached module metadata. Pruned report receipts
are stale; attempts and ledger entries remain durable.

Cancellation outcomes are `cancel_requested`, `already_terminal`,
`identity_mismatch` or `unknown`. Timeout/malformed/empty replies retain intent
and backoff; terminal claims require identity-bound observation. Mismatch suppresses
repeated cancellation until an explicit new request. Acceptance alone cannot refund
usage. Small journal/validation/fsync work remains synchronous: use private local
storage; arbitrary slow filesystems can still delay the process.

## SSH, HTTP and shutdown

OpenSSH receives reviewed identities/hosts/session settings, no forwarding or
agent-controlled options. Helpers require the owned authenticated master; its
failing fallback cannot initiate another login. Streams use backpressure, finite
bytes and whole-operation deadlines. Timeout/overflow/cancellation stops and
reaps the local process group, including a child created during cancellation.
Kernel uninterruptible waits cannot be forcibly bounded by Python.

HTTP validates the configured origin, rejects redirects/environment proxies and
compressed bodies, and bounds streamed bytes/time. Arbitrary mutations are not
replayed after timeout. Output publication's narrow unchanged-request retry is
specified in [output recovery](data-transfer.md#output-recovery-without-another-compute-run).

## Outage recovery

Controller/helper failures share jittered exponential gates, capped at 60 seconds.
One operation probes each cooldown; an older success cannot clear a newer failure.
HTTP retries occur at durable polling/report boundaries. Controller 401/403 stops
the connector rather than retrying credentials.

After master loss, drain operations while retaining intents/reports, retire the
owned socket, and check TCP without credentials. Checks use roughly 2, 4, 8, 16,
32, then up to 60 seconds with jitter, at most twelve probes per outage. Remove
only an unchanged owned socket refusing local connection; never a replaced or
still-listening one.

Automatic SSH authentication requires current saved owner permission immediately
before each attempt. Once reachable, make one noninteractive pinned-host,
explicit-key attempt, disabling password, keyboard-interactive/MFA, agents and
prompts. Failure opens the circuit until human reconnect; attempts are at least
60 seconds apart. Successful renewals restart the finite session lifetime with
no application-wide renewal cap. No password/passphrase/MFA answer is retained.

Recovery keeps worker/native identities and reservations. Previous leases may
need to expire before a new generation reconciles. Shutdown or fatal authentication
failure drains all owned lanes before closing the master; accepted Slurm jobs
continue. Without owner permission, expiry needs explicit local reconnection.

## Verification

Default tests use synthetic credentials, controlled processes and stub schedulers.
The personal-connector fixture adds native parallel submissions, master death,
renewal, unchanged identities and settled accounting. Reproducible commands and
limits are in [container testing](container-testing.md); these tests do not certify
institutional MFA policy or same-account isolation.
