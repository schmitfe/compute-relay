# Execution backends and cluster connections

Status: target backend design; a first-party Slurm/SSH adapter and read-only discovery are now implemented in the development alpha. See [runtime scope](runtime.md) and [container acceptance evidence](container-testing.md) for current validation.
Research checked against primary documentation on 2026-10-21. Upstream `stable`, `latest`,
and default-branch links move; pin dependency releases and commit references when an
implementation adopts upstream code. This document describes planned interfaces and
acceptance criteria, including functionality beyond the current alpha.

## Decision

Implement a small, first-party Slurm adapter over OpenSSH first. Run it in the protected
execution connector, behind the control service's approval, authorization, and budget
transactions. It must work without a scheduler REST API or an always-running process on
the cluster. The local package supports a Linux device and independently saved connectors; future hosted
control and outbound remote connector pairing are later stages.

Use an explicit transport/backend split:

- A **backend** knows scheduler resource requests, submission, native states, and usage.
- A **transport** knows authenticated reachability, bounded command execution, and files.
- A **connector** owns credentials and executes a leased, narrowly authorized operation.
- The **control service** owns projects, approval, the authoritative job state machine,
  budget reservations, attempt identity, and audit history. The connector has no direct
  database access and cannot grant approvals or increase an allowance.

Today: `SlurmBackend + OpenSSHTransport`. A later `SlurmBackend + FirecrestTransport` or
dedicated Slurm REST adapter must pass the same execution and accounting contract tests.
HTCondor and Kubernetes will implement the backend contract independently. Do not force
Slurm partitions, QoS names, or GPU GRES strings into mandatory cross-backend fields.

Native Slurm first keeps the installation small and gives us direct control over the
exact rendered batch script, human-authenticated SSH connection, and ambiguous-submit
recovery. It is a deliberate implementation cost, not a claim that a general-purpose
HPC gateway cannot meet the requirements.

The user-supplied HPC-MCP project is the closest lightweight reuse candidate. Its actual
OpenSSH/Slurm code and security tests justify a focused extraction/adaptation spike;
native-first does not mean rewriting suitable MIT-licensed utilities. It does not change
the protected broker boundary or eliminate our approval, budget, MFA, and durable-attempt
work. See the [pinned source assessment and reuse gates](references/hpc-mcp.md).

## Reuse assessment

| Component | Verified upstream capability | Decision and remaining proof |
| --- | --- | --- |
| HPC-MCP (`XiaozhongFang/HPC-MCP`) | Python stdio MCP service with actual OpenSSH/SFTP Slurm implementation, bounded file/log operations, resource/command policies, remote tracked-job registry, and security tests. | First lightweight reuse spike: extract selected components after focused review or privately adapt a backend. No drop-in approval/compute-credit ledger or verified protected master-only MFA workflow. Preserve our connector/control split and frozen-package contract. [Detailed pinned static review](references/hpc-mcp.md) |
| FirecREST v2 | HPC API with Slurm SSH, REST, or hybrid connection modes; documented static SSH credentials and an SSH credential-service integration. | Evaluate as an optional private gateway. Existing clusters need not already expose REST. Do not make it a local-install prerequisite. Prove the exact script is preserved, interactive university MFA works, and a broker credential cannot bypass project policy. [Configuration](https://eth-cscs.github.io/firecrest-v2/setup/conf/), [command execution](https://eth-cscs.github.io/firecrest-v2/setup/arch/systems/) |
| AiiDA | A transport abstraction for remote operations, distinct from scheduler plugins; persistent scientific workflow management and provenance. Its SSH documentation provides an authentication-script hook for some two-factor schemes. | Reuse the architectural separation and consider optional workflow integration. The hook does not establish Cisco interactive SSH compatibility. Adopting the full engine requires a separate decision about provenance/workflow scope. [Transports](https://aiida.readthedocs.io/projects/aiida-core/en/stable/topics/transport.html), [SSH authentication](https://aiida.readthedocs.io/projects/aiida-core/en/stable/howto/ssh.html), [overview](https://aiida.readthedocs.io/projects/aiida-core/en/stable/intro/index.html) |
| PSI/J | Common job and executor interfaces; the documented Slurm executor uses `sbatch`, `squeue`, and `scancel`; its `url` parameter is unused. | Useful reference and possible future library integration. It does not provide the required SSH/MFA boundary by itself. Prove deterministic preparation before adopting a submission-time renderer. [Slurm executor API](https://exaworks.org/psij-python/docs/v/0.9.11/.generated/index.html) |
| Parsl | Providers for Slurm, Condor, and Kubernetes; providers provision allocations containing workflow workers. | Optional workload engine inside a budgeted allocation, not the first broker scheduler layer. Charge the entire allocation and its workers, not only individual Python tasks. [Execution providers](https://parsl.readthedocs.io/en/latest/userguide/configuration/providers/providers.html) |
| REANA | Job-controller interface and Kubernetes, HTCondor, and Slurm integrations; documented non-Kubernetes build variants include site-specific components. | Study job-controller contracts. Do not assume its Slurm/CERN integration works unchanged at our sites or adopt the whole analysis platform solely to submit jobs. [Job controller](https://reana-job-controller.readthedocs.io/en/latest/) |
| Existing Slurm MCP servers | Discovery, job/log tools, and human-established SSH connection reuse exist. One examined server explicitly exposes arbitrary SSH commands and says its command guard is not a security boundary. | Reference tool ergonomics and MFA failure handling. Do not expose its broad shell tools behind our project key or assume its guards supply a submission-policy layer. [Slurm MCP server](https://github.com/Charlie-Z-work/slurm-mcp-server) |

Reusing a library or gateway never transfers our budget or approval authority to it.
If FirecREST is deployed, keep its endpoint and downstream credentials private to the
connector. Agents authenticate only to the broker. Review upstream licenses, transitive
dependencies, security history, and supported releases before copying or embedding code.

### Gateway adoption gates

Adopt FirecREST or another execution library only after a proof records all of these:

1. Submit a CPU and GPU job using the internal cluster's ordinary SSH access without
   changing cluster authentication policy.
2. Establish the university SSH connection with human MFA; reuse it; observe expiry;
   recover a job after reconnecting. No stored MFA seed or unattended bypass is required.
3. Render before approval, preserve the exact approved batch bytes, and identify all
   gateway-added launch wrappers or scheduler-option overrides.
4. Round-trip a unique broker attempt marker and native job identity; recover a
   disconnect after scheduler acceptance without blindly submitting another job.
5. Obtain final allocated CPU/GPU runtime, handle accounting lag, and cancel a broker job.
6. Keep all downstream credentials and administrative operations inaccessible through
   an agent credential; satisfy local protected-installation requirements.
7. Demonstrate a maintainable deployment, pinned version, compatible license, and passing
   contract tests. Until then, the gateway remains an experiment behind a feature flag.

Failure of gate 2 is a no-go for replacing the university SSH connector. Failure of gates
3–6 is a no-go for use behind unattended budgeted execution. A gateway can still be
supported for other connections whose requirements it satisfies.

## MVP capability boundary

| Capability | First implementation |
| --- | --- |
| Scheduler and connection | Slurm over OpenSSH; key-only internal connection; human-established MFA connection for university |
| Job shape | One node, one batch attempt, explicit CPU count, total memory, whole-minute walltime, optional whole GPU count/type |
| Resource options | Validated partition, Slurm account, QoS, and an administrator-defined constraint identifier; no raw scheduler flags |
| Environments | Approved module profile, pre-existing environment, or an explicitly prepared and authorized build job |
| Outputs | Bounded stdout/stderr reads and selected regular output files beneath the attempt's output directory |
| Accounting | Allocated CPU seconds and GPU seconds; GPU type kept explicit; whole-node rounding configured per partition |
| Unsupported initially | Arrays, heterogeneous jobs, multi-node/MPI launches, Slurm federation, interactive allocations, notebooks, arbitrary `srun`/`salloc`/`ssh_exec` RPCs, user-provided `#SBATCH` directives, automatic retries, agent-requested requeue, fractional/MIG GPUs, job resizing, and Slurm-native dependency graphs |
| Later backends | HTCondor, Kubernetes, FirecREST, and `slurmrestd` adapters are planned extension points, not implemented claims |

Reject unsupported features at validation. Do not silently flatten an array into one job,
discard a constraint, or accept a backend-specific field that changes execution cost.
Permit ordinary `srun` launched by the approved script *within its existing allocation*
only as part of the declared execution profile; there is no agent tool for requesting a
new interactive allocation. Arbitrary workload code remains subject to the remote-UID
limitations below.

## Configuration and discovery

Keep these records separate:

| Record | Examples | Authority and freshness |
| --- | --- | --- |
| Cluster definition | Backend type; administrator-approved login host/port; data-transfer host; remote staging root; trusted command paths | Human-managed configuration, versioned and audited |
| Connection | User's remote identity; connector assignment; protected credential reference; key-only or MFA mode; approved Slurm accounts | Per-user authorization; secret values excluded from normal API responses |
| Capability snapshot | Slurm version, partitions, limits, node resource classes, configured GPU GRES, usable data formats, available environment profiles | Discovery result with timestamp, source command/version, raw-evidence reference, and parser version |
| Access snapshot | Visible account/association/QoS information and denied/unknown queries | Per connection; partial evidence, not permission to use every advertised partition |
| Availability snapshot | Node/partition states and aggregate free resources | Short-lived observation; not a reservation or start-time promise |
| Effective broker policy | Approved clusters, partition/account/QoS allowlists, maximum allocation, project allowance, whole-node accounting rules | Control-service decision; agent input cannot broaden it |

Use `sinfo` and read-only `scontrol` queries for capabilities. Query user associations or
QoS with `sacctmgr` only if the site permits it; lack of permission means `unknown`, not
unrestricted access. `sinfo --all` can display partitions inaccessible to the user, so
discovery must not infer authorization from visibility. [Slurm `sinfo`](https://slurm.schedmd.com/sinfo.html)

Bootstrap probes are bounded, read-only, and explicit: known-host verification, remote
identity, Slurm version, supported structured formats, `sacct` availability/fields,
resource-allocation semantics, and remote filesystem/helper prerequisites. Store redacted
diagnostics. GPU memory, software modules, and local allocation conventions may require
administrator annotations instead of executing discovery code on compute nodes.

Prefer JSON where the installed Slurm version and parser plugin support it; keep
versioned parsers for bounded, explicitly selected text fields. Never parse human-aligned
default tables or assume current upstream JSON is compatible with every cluster release.
Support claims are established by fixture and cluster tests, not by broad version ranges.

Discovery, access checks, and final submission can disagree because cluster policy changes.
Surface the native rejection; do not silently choose another partition or identity.
Availability refreshes must not generate login storms or heavy scheduler polling.

## Planned backend contract

Use typed Python interfaces internally and versioned JSON schemas across the connector
boundary. All operations are scoped to the connector, connection, project, job revision,
and attempt named by an authenticated lease. A native job ID supplied by an agent is
never sufficient authority to inspect or cancel a job.

| Operation | Input | Required result and behavior |
| --- | --- | --- |
| `probe` | Approved connection reference, probe scope | Connection state and supported feature flags; no secret material |
| `discover` | Scope and previous snapshot version | Timestamped capability/access/availability snapshots with unknown fields preserved |
| `prepare` | Validated `JobSpec`, content-addressed inputs, environment profile, immutable site-profile version | `PreparedExecution`: exact batch bytes, all wrappers/setup files, manifest digests, `connection_revision_sha256`, effective `maximum_allocation` and requested walltime, output layout, backend/renderer versions |
| `stage` | Prepared package digest and approved attempt directory | Verified staged manifest/receipt; no job submitted; idempotent for identical content |
| `submit` | One current submission lease, package digest, attempt identity | Native identity, confirmed rejection, or explicit `submission_uncertain`; no implicit retry |
| `reconcile_submission` | Attempt marker, remote receipt, bounded time window, remote identity | Exactly one matching native identity, no conclusive result, or conflict requiring operator action |
| `observe` | Batch of broker-owned native identities | Raw native state, normalized state, observation time, allocation facts, reason/exit details; no budget settlement side effects |
| `cancel` | Broker-owned identity, cancellation lease | Idempotent request receipt; final state and usage remain pending until reconciled |
| `collect_usage` | Native identity, full attempt/restart history | Idempotent allocation records, completeness/finality flags, raw evidence digest; never a fabricated zero for absent records |
| `read_log` | Known stream, cursor, byte limit | Bounded bytes, next cursor, truncation/rotation metadata |
| `list_artifacts` / `fetch_artifact` | Attempt-relative path or manifest entry, byte limits | Authorized regular files with size/digest; explicit refused/changed/over-limit errors |
| `cleanup` | Finalized attempt and retention authorization | Only that attempt's managed files; no user-selected recursive deletion path |

`JobSpec` common fields cover executable entrypoint, arguments, input references, CPU
count, memory bytes, requested walltime seconds, optional GPU count/type, environment profile,
and desired artifacts. Slurm partition/account/QoS live in a typed Slurm options object.
Secret references, SSH flags, hostnames, absolute remote paths, command templates, and
arbitrary option maps are not agent-controlled fields.

Version 1 requires requested walltime to be a positive whole-minute value, expressed as
seconds divisible by 60. Reject other values instead of silently rounding the reviewed
request. Apply partition/site allocation rounding before automatic-execution and hard
per-job caps: a request that fits a cap before rounding can exceed it after rounding.

`PreparedExecution` must bind the connection's remote identity and immutable
`connection_revision_sha256`, site-profile version, resolved environment/build dependency,
package digest, and effective `maximum_allocation` assumptions. Requested
scheduler walltime is the sole reservation duration. No separate charge-duration,
termination, overrun or cleanup field is accepted or added to the reservation.
The control service binds approval or budget authorization to this prepared revision.
Changing any execution-affecting field makes a new revision. If a connector or backend
cannot reproduce the exact package, it fails instead of rendering a new one after approval.

The connector reports scheduler facts; the control service applies durable state
transitions and ledger updates transactionally. Observation/retrieval failure is separate
from workload failure. Transport outages must not mark a running job as failed or free its
reservation.

## OpenSSH transport and MFA

Run OpenSSH only under the protected connector identity. Keys, passphrases, the SSH agent
if used, known-host records, SSH config, and multiplexing sockets belong to that identity
or its protected container volume. Never mount the desktop user's whole home or forward
the desktop SSH agent. Import only administrator-reviewed SSH settings; user-writable
`ProxyCommand`, `Match exec`, `LocalCommand`, or dynamic include files would otherwise
execute code in the secret-bearing connector.

The transport offers fixed trusted operations, file transfer, connection status, and
bounded process results. It offers **no arbitrary shell RPC to agents or the public API**.
Local subprocesses use argument vectors and `shell=False`. This alone does not make a
remote SSH command safe: OpenSSH assembles the requested remote command for execution
on the server, so remote quoting and the trusted-operation boundary need independent
tests. [OpenSSH `ssh`](https://man.openbsd.org/ssh)

Planned implementation: an audited, first-party one-shot remote helper, launched with a
fixed absolute administrator-selected Python 3 interpreter in isolated mode. It accepts
a bounded JSON request on standard input and a closed operation enum, invokes Slurm
with argument vectors and administrator-approved absolute binary paths, and emits
structured responses. Its implementation is shipped with the connector and its exact
source digest is bound in the prepared package and configuration version; agent-uploaded
Python is never imported, evaluated, or executed by this helper. There is no arbitrary
command operation.
The login node needs Python 3 but no installed daemon, service, or broker credential.
The helper performs only lightweight scheduler/filesystem operations; workload execution
and package installation occur in scheduled jobs. If a site cannot support this helper,
an alternative fixed-command/SFTP transport needs the same safety and contract tests before
being enabled; there is no fallback to raw agent shell commands.

The site's remote shell startup files and administrator-installed interpreter/binaries
remain part of the remote trust base. The helper does not make an already compromised
cluster UID trustworthy. Keep local secret isolation independent of remote startup or
workload behavior; never supply local secrets as remote environment variables.

Connection modes:

1. **Key-only:** connect using the protected credential reference with bounded attempts,
   pinned known-host verification, no forwarding, no PTY, and noninteractive authentication.
2. **MFA-required:** a human performs the site's interactive SSH authentication through a
   protected operator channel. The resulting master connection and socket stay under
   the connector identity. Worker operations reuse only this connection. MFA prompts and
   responses are never model context, API job data, durable logs, or reusable secrets.

OpenSSH supports `ControlMaster`, `ControlPath`, and `ControlPersist`, but ordinary
multiplexing may fall back to opening a new connection when the socket disappears.
`BatchMode=yes` suppresses prompts; it does not guarantee that a new authentication attempt
will not occur. For MFA worker calls, verify the master and make fallback transport
creation fail closed, including the race where the master dies after the check. A tested
per-call configuration can disable direct connection creation (for example a failing
fallback `ProxyCommand` with jump settings overridden), while the human login path uses
the legitimate site route. Test this with the actual bastion configuration; do not treat
an `ssh -O check` precheck alone as sufficient. [OpenSSH client configuration](https://man.openbsd.org/ssh_config)

Connection authentication states: `disconnected`, `authenticating`, `ready`, `auth_required`,
`disabled`, `revoked`. Host verification and network diagnostics are reason/details fields,
not additional authentication states. On expiry, enter `auth_required`, stop new submissions
and repeated authentication attempts, and preserve reservations and native identities. A
human reconnects, then the worker reconciles jobs and retrieves results. Slurm jobs
already accepted continue independently of the workstation's power or SSH state.

The local protected-installation operator channel is part of the security/deployment
design. A general terminal endpoint in the agent API is not an acceptable MFA interface.
Container restarts discard live sessions and require human reauthentication. Test VPN,
bastion, keepalive, and device-sleep behavior at each site; compatibility is unproven until
these tests pass.

## Immutable preparation, staging, and submission

Use this sequence:

1. Validate sizes, file counts, entrypoint, resource bounds, and environment requirements.
2. Resolve administrator-owned site/environment profile versions, immutable connection
   revision, and dependency digests. Derive the effective resource allocation after
   site rounding, then check it and requested walltime against automatic/hard
   per-job caps and available allowance.
3. Render all scheduler directives and launch/setup scripts. Review includes exact files,
   resource/usage maximum, environment steps, source references, and outputs.
4. Store an immutable manifest with SHA-256 file digests, normalized specification,
   renderer version, `connection_revision_sha256`, effective `maximum_allocation`, and
   full package digest. Issue human approval or a transactional
   project reservation for that revision. Denial comments are readable by the agent.
5. Stage to a broker-derived remote directory, such as
   `<approved-root>/<project-uuid>/<job-uuid>/<revision>/<attempt-uuid>/`.
   Upload under a temporary name, check digests, then finalize. Identical stage retries
   are safe; conflicting content is an error. No extracted symlinks, hard links, device
   files, traversal paths, oversized archives, or agent-selected absolute destinations.
6. Recheck lease validity, authorization, package digest, and prepared site/connection
   binding immediately before crossing the scheduler submission boundary.
7. Submit the exact batch bytes using `sbatch --parsable` through the trusted helper.
   Persist every observed native response promptly. Never use `--wrap` to synthesize
   unreviewed command text.

Generated directives include an explicit walltime, CPU/memory/GPU allocation, known
partition/account/QoS, controlled stdout/stderr paths, `--no-requeue`, and a broker attempt
marker in a bounded job name and, where supported, comment. Resources are authoritative
in the generated script; strip inherited `SBATCH_*` overrides and use a controlled Slurm
command environment. Reject newlines/control characters in directive values. Site
defaults and trusted wrappers are versioned inputs, not hidden user shell preambles.

Slurm returns from `sbatch` after accepting the script, before resource allocation;
`--parsable` returns the job ID and optional cluster name separated by `;`. Slurm does not
stage ordinary input files for the job, which is why stage verification is a distinct
operation. [Slurm `sbatch`](https://slurm.schedmd.com/sbatch.html)

Package immutability fixes what the broker authorized and what it handed to Slurm. It
does not prove an immutable runtime: mutable cluster modules, external downloads,
same-UID processes, and data changes can affect execution. Digest-pin environments and
copy/version relevant inputs where feasible. Keep references to mutable datasets explicit
in the review. No administrator or whole-remote-UID tamper resistance is claimed.

## Submission identity and ambiguity

Use separate identities for the logical job, revision, and attempt. Each attempt has a
UUID, unique broker idempotency key, one budget reservation, one remote staging directory,
and a native identity composed of connection, Slurm cluster, remote user, native job ID,
and submission time/evidence. A bare integer job ID is not globally or permanently unique.

On the control service, create the attempt/reservation and durable submission task in one
transaction. Repeated identical client requests return the same attempt; reusing the
key for different content is a conflict. Worker lease expiry must not automatically
produce a new submission operation once an attempt may have reached Slurm.

Before invoking `sbatch`, the remote helper writes a create-once attempt intent and takes
a lock scoped to that attempt on the shared staging filesystem. It writes a receipt
immediately after a native ID is observed. This reduces duplicates during connector
restarts, but cannot make Slurm submission and our database one atomic transaction.
Validate lock/rename behavior on the actual shared filesystem; do not assume local-disk
semantics or rely on the lock as an adversarial remote-UID boundary.

An SSH timeout, lost response, malformed response, helper termination, or missing receipt
after contact is **`submission_uncertain`** unless non-submission can be established. Retain
the reservation and serialize recovery for that attempt:

1. Inspect the remote intent/receipt, bounded `squeue`/`sacct` history, attempt marker,
   remote owner, submit-time window, and working directory where available.
2. Exactly one strong match: attach its native identity and continue observation.
3. Multiple matches: record all candidates, block further submission, and require
   operator resolution; do not cancel unrelated candidates speculatively.
4. No match: remain uncertain while accounting/controller records may lag or disappear.
   Absence from the queue is not proof that a short job never ran.
5. Retry `sbatch` only after explicit evidence proves non-submission, or an operator
   resolves uncertainty and authorizes a distinct attempt with its own reservation.

Slurm's ordinary `sbatch` CLI does not provide our application an idempotency transaction.
The promise is durable deduplication plus conservative ambiguity handling, not exactly-once
execution under every failure. Marker collisions, mutated comments, truncated names,
purged accounting, job-ID reuse, and filesystem failures belong in fault-injection tests.

## Observation, accounting, and retries

Batch `squeue` requests for known job IDs instead of polling once per job. A configurable
starting cadence is 15–30 seconds with jitter, slower when idle or failing, and a
site-defined RPC rate cap. Treat this as an operational setting, not a user promise.
Normalize native states while retaining the original state/flags and observation source.
Slurm queue absence means "consult accounting", not "completed successfully".
[Slurm `squeue`](https://slurm.schedmd.com/squeue.html)

Use `sacct` for terminal confirmation and allocation history; accounting can lag. Request
explicit bounded fields and times, prefer supported structured output, and retain evidence.
The allocation-level `AllocTRES`, elapsed seconds, start/end times, native identity,
exit state, and restart history support settlement. Do not sum batch/extern/step rows on
top of the parent allocation, or count both aggregate and typed GPU TRES twice. Do not
substitute process CPU utilization or GPU activity for allocated resource time.
[Slurm accounting fields](https://slurm.schedmd.com/sacct.html)

Usage records need stable deduplication keys and finality/completeness flags. The control
service alone posts charges. Finalize only after a complete terminal record meets the
backend's tested conditions. If accounting is missing or inconsistent, retain the
reservation in an accounting-pending state. The owner may explicitly settle conservatively
against the reserved maximum or resolve it with evidence; there is no automatic zero-use
settlement. A cluster without usable accounting starts in review-only mode with a stated
conservative settlement policy, not unattended actual-usage budgeting.

Site profiles specify whether a request allocates extra CPUs/GPUs or a whole node and
whether suspended time is charged. After site rounding, reserve CPU seconds as the
maximum allocated cores multiplied by requested walltime, and GPU seconds
as maximum allocated devices multiplied by that duration; preserve GPU type dimensions.
Apply automatic-execution and hard per-job limits to these effective values, not only
to the agent's nominal request.

Slurm's `KillWait`, `OverTimeLimit` and site cleanup can extend elapsed allocation time
beyond requested walltime. They are not separate broker configuration or planning
limits. Final accounting charges observed allocated resources and elapsed time in full,
even when they exceed the reservation. The reservation therefore controls admission
commitments rather than guaranteeing a physical ceiling on scheduler usage. An
overdrawn project reports debt and cannot admit new work until the debt is resolved.
[Slurm time-limit configuration](https://slurm.schedmd.com/slurm.conf.html)

If effective resource allocation or requested walltime is unknown, submission remains
blocked until the request and cluster configuration provide finite values. Missing or
ambiguous final accounting preserves the reservation until reconciliation. Usable
observed overage must be settled rather than discarded or capped at the reserved amount.

No automatic retry or agent-requested requeue in the MVP. A retry is a new explicit
attempt with a new admission decision and reservation. Scheduler-forced requeues are
still possible at some sites: `ForceRequeueOnFail` can override `--no-requeue`, and a
requeued job restarts its batch script. Record every observed restart/segment, do not
release an earlier charge, and block unattended use if the total allocation cannot be
bounded by the site's configured policy. Do not assume a changed Slurm `Submit` time
means a new broker attempt. [Slurm requeue options](https://slurm.schedmd.com/sbatch.html),
[Slurm duplicate accounting records](https://slurm.schedmd.com/sacct.html)

Future arrays need cardinality validation, maximum concurrent elements, full worst-case
reservation across all elements, per-element terminal/accounting reconciliation, and
idempotent cancellation. They cannot be enabled by allowing a raw `--array` field.

## Environments and output handling

An environment profile records interpreter/module/container versions and exact setup
commands. Agents may request a custom environment definition, but building it is an
executable workload. Prepare and approve/reserve a separate build attempt; run it on
compute resources or an explicitly authorized build backend. Dependency installation,
container builds, imported setup hooks, and tests never run in the credential-bearing
connector, control service, or on the login node as arbitrary agent work.

Cache environments by a complete specification digest plus relevant platform/driver
metadata. A cache hit still needs project access checks; do not share private package
contents merely because a digest matches. A generated environment/image artifact must
be finalized and its digest bound into the subsequent execution revision. Prefer pinned
Python dependencies and immutable Apptainer images when sites support them; verify GPU
driver/runtime compatibility in cluster-specific acceptance tests. Mutable module profiles
are labeled as such in the review. [Apptainer GPU support](https://apptainer.org/docs/user/latest/gpu.html)

The remote helper performs filesystem operations relative to approved directory handles,
checks each path component, and uses no-follow open semantics. Agent paths are relative
to the attempt, bounded in length, and never interpreted as shell syntax. Output retrieval
rejects symlinks, devices, sockets, traversal, and files outside the attempt output root;
uses byte/file-count ceilings; and checks that a file has not changed during a claimed
digest transfer. If the remote platform cannot provide required no-follow semantics,
automatic artifact retrieval fails closed until an alternative passes the tests.

Logs expose cursor/offset and truncation/rotation metadata, not an unbounded `cat`.
Large artifacts remain remote by default. Artifact text and file names are untrusted
model inputs and never become connector instructions. Preview HTML and other active
content only in a sandboxed viewer. Cleanup uses retained broker-owned directory identity
and a retention authorization, never an agent-supplied recursive path.

### Artifact transport extension

**Implemented today:** `collect` retrieves bounded declared regular files through the
protected SSH worker and reports their bytes to the broker. The broker stores those
outputs under its quotas and serves authorized downloads. There is no implemented
direct endpoint, configurable route selector, transfer-token API or object-store adapter.
This design work does not change the current helper, prepared manifests or lab execution.

**Planned:** separate artifact descriptors/authorization from the data transport. The
control service remains authoritative for project access and retains the attempt,
logical path, size, SHA-256, source version/receipt and delivery audit. The selected
adapter carries bytes using one of these capabilities:

| Adapter capability | Planned behavior |
| --- | --- |
| `relay` | Existing cluster → worker → broker → client path, with broker storage and transfer limits |
| `direct` | Site-approved artifact endpoint serves the descriptor's bytes directly to the authorized analysis client; the broker carries only metadata and transfer receipts |
| Optional `object_store` | Cluster uploads the descriptor's bytes to a configured HTTPS object-store destination; the client downloads them using separate object-specific authority |

Do not assume an SSH-only connection can provide direct transfer. A direct adapter needs
an approved cluster/data-transfer endpoint that can validate a narrowly scoped grant;
the client should normally initiate its connection rather than require an inbound port.
Its credentials are independent of the broker's SSH keys and human-established MFA
connection. An adapter must never export those keys, sockets or reusable login authority
to the agent/client. A site that lacks an acceptable endpoint continues using relay.

Connection capability/policy defines named endpoint IDs and allowed routes. Project
policy may narrow these with preference mode `direct`, `relay` or `auto`. Fixed modes
return a structured error if their route cannot be used. `auto` tries only a configured
preferred route and an explicit ordered fallback list; an unavailable direct connection
does not grant permission to relay or upload elsewhere. For example, an explicitly
allowed relay fallback handles a firewall that blocks client access to the cluster
endpoint. The optional object-store route requires its own explicit policy enablement
and configured destination; outbound HTTPS alone does not authorize a new destination.
See [route authorization and selection](architecture.md#artifact-delivery-routes-implemented-relay-and-planned-selection).

The future adapter surface can stay small:

| Operation | Required boundary |
| --- | --- |
| `describe_artifact` | A declared attempt-relative regular file; bounded no-follow metadata/hash inspection; return size, digest and stable source receipt, never an arbitrary path |
| `plan_transfer` | Descriptor plus effective route policy and authenticated client identity; report configured endpoint/route capability or a structured blocked/unavailable reason; no arbitrary network probes |
| `start_transfer` | Current bounded artifact-only grant; permit just the descriptor and configured endpoints, with expiry, audience, byte/concurrency and redemption limits |
| `observe_transfer` / receipt | Transfer ID and descriptor digest; progress/finality/integrity result; no scheduler or budget side effects |

These operation names are a proposed contract, not methods already exposed by the helper.
Metadata hashing or export preparation must obey site I/O rules; if it needs a scheduled
workload, create a separately authorized and budgeted attempt. A direct endpoint must
serve a stable file snapshot/descriptor or detect mutation, and the destination must
verify the expected size and hash before accepting the artifact. Preserve the existing
remote same-UID trust limitation: transport checks do not make mutable cluster files
tamper-proof. Transfer failure, expiry or an unknown delivery receipt remains separate
from job completion and compute settlement.

Use installation-configured endpoint references only. Agents cannot supply an HTTP URL,
SSH command, hostname, tunnel or destination path. Enforce TLS/host/port allowlists and
restricted egress/DNS resolution; reject redirects to unapproved addresses and access to
local/metadata/unrelated private endpoints. Object-store grants cover one approved
object and short-lived upload/download operations, not bucket listing or general cloud
credentials. Direct delivery does not consume broker disk quota for absent bytes, but
still needs metadata quotas, transfer limits and audited retention/deletion policy at
the actual source/destination.

Future conformance cases must include expired/revoked/wrong-audience tokens, replay and
parallel byte-limit enforcement, hash/size mismatch and remote mutation, denied paths
and cross-project access, blocked direct routes with forbidden/explicit relay fallback,
destination allowlist and redirect/DNS attacks, object-store expiry, and confirmation
that a direct transfer leaves no artifact body in the broker. These routes remain
disabled until their adapters and site-specific acceptance tests exist.

## Security and budget limits at the cluster boundary

Protecting local broker secrets does not isolate arbitrary Python from the **remote
cluster user account**. A submitted process can generally read that account's files,
invoke scheduler clients, modify its own jobs, or start additional allocations if the
site allows it. Existing credentials in the remote home can be exposed to that process.
Remote same-UID processes can also alter staged files; file modes and content hashing
do not remove this authority.

Therefore the MVP enforces an admission/reservation limit on broker-managed submissions,
with conservative accounting and no direct agent scheduler API. It does not guarantee a
hard total ceiling for every process or nested job under the remote UID. Filtering
strings such as `sbatch` from Python is not an effective solution. Local Docker secret
isolation does not change cluster-side permissions.

For adversarial workloads or a hard whole-account ceiling, require additional site-backed
controls before claiming that guarantee: a suitable dedicated identity, scheduler-enforced
account/QoS/resource limits, submission-policy hooks or equivalent controls, and verified
restrictions on credentials/network access available to job code. A dedicated identity
alone still permits nested submission unless the site restricts it. Document which
controls each connection actually has and expose that assurance level in project policy.
Do not upload broker SSH credentials, service tokens, or human MFA information into jobs.

## Backend verification and cluster acceptance

The repository's test plan should include these backend-specific cases:

- **Unit/property tests:** deterministic scripts/digests; connection-revision binding;
  whole-minute walltime validation; resource/time rounding and cap checks after rounding;
  reservation multiplication by requested walltime; final accounting beyond that
  reservation and debt blocking new admission; rejection of missing walltime; rejection of
  unknown flags and newline/control-character injection; shell-quoting boundaries; strict
  native ID parsing; state mapping; TRES parsing without double counts; path traversal,
  symlink swaps, output limits, and cache permission checks.
- **Versioned fixtures:** discovered capabilities, inaccessible association queries,
  GPU/no-GPU nodes, failed/pending/completed/OOM/cancelled jobs, accounting lag, steps,
  duplicate records, job-ID reuse, unexpected requeue, unknown native state, non-ASCII
  diagnostics, and login banners. Record fixture provenance and scrub real identifiers.
- **SSH integration:** protected key/socket ownership, host-key change rejection,
  no inherited user SSH configuration, no agent forwarding, key-only execution,
  expired MFA socket, and master-disappears-between-check-and-use. Assert no fallback
  authentication attempt occurs and no secrets reach logs/API responses.
- **Submission fault injection:** before intent, during staging, before `sbatch`, after
  controller acceptance, before receipt, before response persistence, after lease expiry,
  and with two workers racing. Unknown states retain the reservation; no blind duplicate.
- **Real Slurm smoke tests:** ephemeral test cluster for submit/observe/cancel/accounting,
  exact executed package, failed script, fast completion, walltime kill, GPU request
  rendering, and worker/control restart recovery. A CPU-only fixture cannot prove GPU
  scheduling or driver compatibility.
- **Site acceptance:** a human configures disposable test credentials and approves tiny
  workloads for each real cluster. Record Slurm version, partition/allocation behavior,
  GPU accounting, site load limits, filesystem locking, MFA expiry, and observed handling
  of requeue. These tests are opt-in and never use production credentials in public CI.
- **Future adapter conformance:** run the same authorization, package, submission,
  uncertainty, cancellation, and usage contracts against any new gateway/backend;
  retain explicit unsupported-capability tests instead of silently reducing semantics.

Publishing the repository does not imply real-site support before the corresponding
acceptance record exists. Default CI needs no institution, public network cluster,
secret, MFA enrollment, or paid allocation.
