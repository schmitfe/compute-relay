# Bounded asynchronous Slurm workers

The existing synchronous runner remains the default and supports the optional
extensions. An installation can explicitly select the asynchronous **core Slurm**
runner with `BROKER_WORKER_ASYNC=1`. It uses `AsyncJsonClient`,
`AsyncSlurmBackend` and `AsyncOpenSSHTransport`; SSH and HTTP operations use
asynchronous sockets/process pipes, without a thread-pool wrapper or transport
retry. The worker extra must include `httpx`.

An async worker requires the owning broker user's UUID in its protected
`owner_id` configuration field or `BROKER_WORKER_OWNER_ID`. This is operator
configuration bound to the worker's scoped credential, not a principal selected
by an agent request. Existing connection/profile/token/journal settings are
unchanged. Preserve the journal directory when changing runner mode: its durable
submission intent and receipt records are shared by both implementations.

## Supported operations and compatibility

Core submission, reconciliation, observation, cancellation, bounded output
collection and connection discovery have awaitable implementations. Prepared
package verification, generated batch files, remote helper source, native job
identity checks and accounting parsers remain shared with the synchronous path.
The async runner explicitly advertises an empty extension capability list.
Configuring extensions on that runner fails at startup; it does not execute a
synchronous plugin on the event loop. Use the synchronous runner for storage,
environment builds and workflows until those plugins have async hooks.

No new extension package is dispatched by the async runner. Observation and
cancellation of previously submitted jobs still use the native identity fence;
an old extension-owned allocation can still have core accounting settled, but
plugin-specific finalization requires a compatible synchronous worker. This
initial runner supports Slurm only; the fake backend keeps its synchronous path.

Local package validation and the small journal read/write/fsync operations are
still synchronous. Put the journal on protected local storage. This is not a
claim that arbitrary network filesystems or large Python serialization tasks
cannot stall a process; the change removes blocking SSH and HTTP waits.

## Owner lanes and bounds

`PrincipalScheduler` maintains an explicit protected owner registry. Its default
limits are four active operations, sixteen active/waiting operations and sixty-four
registered owners. All are finite validated settings; a full queue or unknown
owner is rejected before starting its operation. One operation per owner runs at
a time, including observations. This is stronger than allowing one mutation per
owner. A waiting operation holds its owner's lane before taking a global slot,
so queued work for one owner cannot consume every slot needed by other owners.

The single-owner command entry point reads optional `max_concurrency` and
`max_pending` configuration values. A protected multi-owner launcher can construct
separate owner-scoped clients/configurations/journals and pass workers sharing
one scheduler to `serve_workers()`. That helper supports a fixed list of at most
sixty-four workers; each polls independently and shutdown cancels/reaps active
operations. It does not discover user credentials, enroll owners from requests,
or start an unbounded task per submitted job.

These lanes coordinate one worker process. Database admission transactions,
generation-fenced control leases and the existing attempt journal remain the
authority across processes or replicas. A scheduler is not a distributed quota
or lock. Local journal locks use nonblocking `flock` attempts with a finite wait,
so an older runner holding an attempt fence cannot block every owner's event
loop. Separate worker credentials must remain bound to their actual owner on
the control service.

## SSH and cancellation boundaries

Both transports construct the same fixed OpenSSH argument vector from protected
configuration. They pin known hosts, disable agent/X11 forwarding and local
commands, use batch authentication, and inherit only fixed `PATH`/`LANG` values.
An explicitly configured key is used with `IdentitiesOnly` and no ambient agent.
When no key is configured, `IdentityFile=none` prevents a fallback to default
home-directory keys. MFA connections require an already-established protected
control socket; a missing/dead master retains the `/bin/false` proxy fallback,
so the worker cannot initiate a fresh interactive SSH/MFA login.
The identity settings follow the [OpenSSH configuration
contract](https://man.openbsd.org/ssh_config#IdentityFile).

The asynchronous transport streams input in 64 KiB chunks with backpressure.
It concurrently drains stdout and stderr, retaining at most 20 MiB of structured
stdout and counting/discarding at most 64 KiB of diagnostics. The request limit
remains 150 MiB, and the whole operation uses the configured finite command
deadline. SSH diagnostics are not included in public errors or reports.
Concurrent pipe drainage follows [Python's subprocess
guidance](https://docs.python.org/3/library/asyncio-subprocess.html#asyncio.subprocess.Process.wait).

Each SSH subprocess gets a new local session. Timeout, output overflow or
cancellation kills its operation's local process group and awaits direct-child
reaping. Process creation is shielded long enough to retain ownership of a child
created concurrently with cancellation; cleanup completes even if shutdown
requests arrive repeatedly. No transport call retries automatically. As with any
process timeout, kernel operations stuck in uninterruptible sleep are outside
Python's ability to force immediate reaping.

Cancellation after dispatch authorization leaves the already-fsynced submission
intent intact. It does not claim that the remote scheduler failed to submit.
After restart or a resumed lease, the worker reconciles that same attempt instead
of repeating `sbatch`. A successful submission receipt is persisted before its
HTTP report, so a lost report replays that receipt. Terminal output collection
failure still allows valid final accounting to reach the control service.

The async HTTP client verifies the configured HTTPS origin, disables environment
proxies and redirects, rejects compressed bodies, enforces a total request
deadline and streamed byte limit, and uses the existing strict JSON parser. It
does not replay dispatch/report mutations following a timeout.

## Verification

The unit and SSH suites use first-party local subprocess fixtures and a stub
scheduler, with no real SSH destination or Slurm allocation. They cover
bidirectional backpressure, concurrent owner lanes, queue bounds, subprocess
timeout/output overflow/cancellation, HTTP redirect and stream limits, journal
contention, durable intent recovery, receipt replay and terminal output failures.
A complete default async worker/backend/transport fixture invokes only the fixed
helper through an explicitly configured test SSH shim; uploaded job source is
staged as data and never executed by that shim or connector.
