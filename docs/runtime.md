# Development alpha runtime

This document describes the implemented code. The architecture, API, deployment, and testing documents describe the broader target; their unimplemented endpoints and acceptance gates are not promises of current functionality.

The [shared-service specification](hosted-architecture.md) separates the implemented
PostgreSQL, local/LDAP identity and user-bound worker baseline from remaining
hosted deployment work. Keyless central-session activation, protected installation
isolation, OIDC and membership-management screens remain unimplemented.

## Components and startup

Python 3.12/3.13, Django 5.2, SQLite or PostgreSQL, system OpenSSH, the official MCP Python SDK, JSON Schema, and RFC 8785 canonical manifests form the runtime. Optional extras add the PostgreSQL driver, LDAP adapter and async HTTP client. `requirements.lock` pins the resolved versions. Docker installs each selected extra under these constraints; base-image digests, artifact hashes, license inventories, and release provenance remain release work. No upstream HPC-MCP source has been copied or executed.

| Entrypoint | Purpose |
| --- | --- |
| `python -m compute_broker.cli bootstrap-demo --output local/demo --backend fake --fake-state-directory /absolute/private/fake-state` | Apply shipped migrations and create a synthetic owner, project, connection, environment, project token, worker token/config, and allowance |
| `python -m compute_broker.control` | Development HTTP service, loopback port 8765 by default |
| `python -m compute_broker.worker` | Protected-worker process; reads only its configured connection/token and journal |
| `BROKER_WORKER_ASYNC=1 python -m compute_broker.worker` | Opt-in asynchronous core Slurm runner; requires protected owner configuration and no enabled extensions |
| `python -m compute_broker.mcp` | Project stdio MCP facade; has no SSH, approval, or project-administration tools |
| `compute-broker init-mcp` | Initialize the current science project's local TOML/key files and Git ignore rules; prompts privately for the project API key |
| `compute-broker connectors add NAME --state-directory PATH` | Save a private shortcut to an already paired personal connector; stores only a name and path |
| `compute-broker connectors start NAME --verbose` | Start that connector in the foreground with local SSH/MFA prompts and sanitized activity messages |
| `compute-broker connectors start --all` | Authenticate saved connectors sequentially, then run them together in one labeled foreground group |
| `python -m compute_broker.cli discover --host user@login.example --known-hosts /absolute/pinned/known_hosts --output local/discovery.json` | Read-only Slurm capabilities snapshot; no submissions, node probes, or job execution |
| `python tools/local_smoke.py` | Disposable native fake end-to-end acceptance and automatic process/state cleanup |

Run from the installed environment; use `.venv/bin/python` in a checkout. The demo bootstrap is intentionally synthetic. Its `--backend slurm` selects only the disposable fixture, never an institutional host. Repeating bootstrap with existing metadata reuses its project; it does not rotate credentials or extend the project admission/key's seven-day expiry. Worker credentials do not yet have automatic expiry. Use a fresh demo directory/database for a clean run.

For interactive native development, set `BROKER_DATA_DIR` to a private absolute path. Bootstrap a fake project, then start control and worker in separate terminals. Read the generated owner details in the explicit private `demo.json` file to sign in at `/login/`. Do not pass that file to an agent. The supplied smoke client deliberately holds both authorities solely for acceptance testing.

Worker variables are `BROKER_CONTROL_URL`, `BROKER_WORKER_CONFIG`, `BROKER_WORKER_TOKEN_FILE`, and `BROKER_WORKER_JOURNAL`. Its private configuration accepts a `credential_profiles` map of operator-approved aliases; the worker advertises only safe target metadata for browser enrollment. A legacy `connections` map keyed by UUID remains supported for existing jobs. Use a private absolute path for fake state too. See [portal and worker profiles](management.md) for enrollment and a complete example.

MCP configuration needs only `BROKER_CONTROL_URL` and `BROKER_PROJECT_TOKEN_FILE`. Save the one-time project API key in a private file outside version control and configure its absolute path; do not paste its contents into the conversation. The adapter authenticates with `GET /api/v1/context` at startup and derives the project from the key. It inserts `project_id` into job and workflow-node requests internally, so the agent can omit it too. A supplied project ID must match; optional legacy `BROKER_PROJECT_ID` is an assertion checked against the authenticated project, never a selector. Invalid, expired, revoked or unauthorized keys fail startup. The read-only `get_project_context` tool refreshes project identity, key scopes and the key's execution-mode preference; effective approval still follows project/site policy. Other requests continue to authenticate independently, so startup discovery does not bypass later revocation or policy checks.

The [stdio configuration example](../examples/mcp-stdio.json) supplies generic server-launch fields for clients that accept a local stdio server. Remote HTTPS MCP transport for hosted clients remains future work; this alpha does not implement an HTTP MCP endpoint. HTTPS is mandatory outside loopback unless `BROKER_ALLOW_INSECURE_HTTP=1` explicitly enables the isolated development network.

Control variables include `BROKER_DATA_DIR`, `BROKER_DB_PATH`, `BROKER_ARTIFACT_ROOT`, `BROKER_SECRET_KEY_FILE`, `BROKER_HOST`, `BROKER_PORT`, and `BROKER_ALLOWED_HOSTS`. Its random Django secret is generated once in its data directory with mode 0600. The service currently accepts only `BROKER_PROFILE=development`. Selecting a protected profile fails closed because its installation acceptance is incomplete. Django's development server is for these local tests; it is not the hosted service deployment.

Optional apps are selected explicitly with `BROKER_EXTENSIONS=storage,environments,workflows`.
The default is empty. Each app owns its tables, migrations, APIs, portal and
worker adapter; worker and MCP enablement are independent. Project settings
cannot install or enable an app. See [the extension contract](extensions.md)
and [core-only acceptance](module-boundaries.md). Migration checks and CI cover
both the core and enabled extensions.

## Database and human identity configuration

The ordinary Slurm setup also supports a [personal connector](personal-connectors.md).
Save the user's own cluster and local key/known-hosts path hints, download its
ten-minute one-use pairing file and start `compute-broker connector` on that
user's device. OpenSSH handles interactive host-key/passphrase/MFA prompts;
after successful login, pairing creates the internal worker identity and
discovery binding automatically. The database retains only pairing/worker
token hashes. Local private state preserves the worker credential and journal
for resume. Background operations reuse the private master socket without
starting a new login. Normal termination and the configured deadline close the
owned master. This core Slurm connector does not enable optional extension
workers or establish same-user OS isolation.

Personal setup now continues after discovery: **Cluster configuration** supplies
missing resource defaults from discovered single-node ceilings, and its
owner-only directory browser uses leased, bounded, read-only requests through
the running connector. Final confirmation validates the selected work root,
persists a new local profile and waits for its acknowledgement before admitting
jobs. Configuration changes are blocked until accounting is settled. These
human operations are not MCP tools or project-key capabilities. Module auto
discovery uses trusted path data and a provisional filename catalog without
evaluating modulefiles. See [cluster configuration](cluster-configuration.md)
and [module discovery](module-discovery.md).

SQLite is selected by default or by `BROKER_DB_ENGINE=sqlite`, using the existing
`BROKER_DB_PATH`. PostgreSQL is selected explicitly with
`BROKER_DB_ENGINE=postgresql` and the `postgres` dependency extra. Configure
`BROKER_DB_NAME`, `BROKER_DB_USER`, `BROKER_DB_HOST`, optional `BROKER_DB_PORT`
and `BROKER_DB_PASSWORD_FILE`. The password file must be private; inline password
variables and DSNs are rejected. `BROKER_DB_SSLMODE` defaults to `verify-full`;
`BROKER_DB_SSLROOTCERT` selects a trusted CA. An explicit disabled TLS mode is used
only for isolated loopback development fixtures. SQLite transaction options are
not passed to PostgreSQL.

The [offline migration tools](database-migration.md) preserve source UUIDs,
password hashes, session rows, exact relationship records, approvals, ledger and
artifact metadata and verify retained object bytes. A new database engine is not
a data migration. The procedure requires stopped writers, paused projects,
resolved work, an empty migrated target and the original Django secret. Legacy
keys without verified issuing-user provenance are quarantined and require an
explicit reviewed owner mapping. Switching Django authentication backend paths
still requires browser sign-in again even when session rows and the secret are
preserved.

The sole `IdentityRoutingBackend` selects a local provider or an explicitly
configured LDAP provider. Omission of `BROKER_IDENTITY_CONFIG_FILE` enables local
login only. Existing local accounts use Django password verification; LDAP users
receive unusable local passwords and cannot fall back to a same-name local login.
Install the `ldap` extra and use a protected provider configuration to enable
verified LDAPS or mandatory StartTLS. Provider ID plus canonical `entryUUID`
identifies an external account; a matching username or email does not link it to
an existing user. First directory login creates no project, worker or HPC grants.

Cached directory eligibility expires within the configured interval, capped at
300 seconds. Human authority and issuing-user checks fail closed when verification
expires or access is withdrawn. The optional protected identity-refresh process
does directory I/O outside database transactions; an outage does not renew
authority or clear operator suspension. Current eligibility supports explicit
POSIX group membership, Samba account flags and shadow expiry. Automatic
group-to-project role mapping, membership-management screens, OIDC and login rate
limiting are still future work. See [identity configuration](../src/compute_broker/control/identity/README.md)
and the independent [synthetic LDAP/TLS fixture](../deploy/ldap/README.md).

The synchronous worker remains the default. The [async worker](async-workers.md)
uses bounded HTTP/SSH I/O, per-owner lanes and the same immutable packages,
dispatch confirmation, journal and accounting protocol. It supports core Slurm
operations only, advertises no extension capability and rejects enabled
extensions. Storage, environment-build and workflow extensions continue to use
the synchronous worker. The async entry point does not install an isolated
multi-user supervisor or establish new SSH/MFA authentication.

## Submission and permissions

Upload logical input files, create a job revision, prepare its immutable package, then request execution. A prepared package binds its request, exact source/generated files, environment revision, connection revision, helper implementation, effective resource bounds, and reservation into a canonical SHA-256 digest. Preparation does not contact Slurm or execute source. Changing source requires a new revision. Human approval binds that digest to one execution request; repeats require new requests and approval or automatic budget admission.

The review page displays every input/generated file as escaped plain text with its digest, the complete manifest, maximum reservation, and rejection comment. Binary input previews use replacement characters, so their hashes and original byte objects remain authoritative. No syntax, HTML, scripts, or artifacts are executed in the UI. Human decisions use a session with CSRF protection. An agent that controls that same human's browser can operate the session; separate credentials alone do not prove human presence. A protected deployment must provide a trusted approval device/channel.

| Project scope | Authority |
| --- | --- |
| `objects:write` | Upload bounded input bytes |
| `jobs:write` | Create, revise, prepare |
| `jobs:execute` | Request execution; never approve |
| `jobs:read` | Read targets, allowance, job/package status and logs |
| `jobs:cancel` | Request cancellation |
| `artifacts:read` | List/download declared collected output objects |

The human portal creates and edits projects, compute and submission limits, cluster/environment allowlists, and multiple scoped API keys. Issuance and rotation reveal a key only in that response; the database stores its SHA-256 hash and an independent UUID identifier. Sessions, audit records, idempotency responses and ordinary pages do not retain the value. Lost keys must be rotated. Revocation retains a tombstone for job attribution and audit. Synthetic bootstrap exports a separate client token file only for the disposable acceptance fixture; this is not the normal key-management path.

All keys share their project's allowance, lifetime total submission cap and simultaneous-job cap. A key can inherit project execution policy, always require confirmation, or request automatic execution within project policy. Automatic never overrides a project requiring review. Setting a key to require confirmation also fences an admitted automatic attempt that has not obtained dispatch permission. Jobs already dispatched retain accounting and observation. Normal agent APIs never expose worker credentials, SSH paths, host authentication configuration, or a general command runner.

Each key has an issuing user. Current account eligibility and project roles are
checked when the key is used and again for execution authority. Its allowed
connection must be complete, enabled, owned by that user and assigned to an
unrevoked worker owned by the same user. Sharing a project's allowance does not
delegate another member's SSH identity. Only the issuing user can rotate and see
the replacement key; project administration does not permit impersonation.
Membership and cross-member project-resource grants currently require reviewed
operator management rather than a complete membership UI.

Agents supply each job's `name` (1–80 letters, digits, underscores, dots or hyphens). The global and project job tables show that name, the owning project/user, the submitting key and latest execution status. Ownership is the project owner's identity; it is not a claim that a human personally submitted an agent job. The package's immutable `prepared` state is separate from the execution status.

Implemented agent endpoints are `/api/v1/connections`, `/api/v1/projects/{id}/allowance`, project `/objects`, `/api/v1/jobs`, job detail, `/package`, `/prepare`, `/revisions`, `/execution-requests`, human-session `/decisions`, attempt `/cancel`, `/logs`, `/artifacts`, and `/api/v1/artifacts/{id}/content`. Mutation requests other than content-addressed uploads need an `Idempotency-Key`; reusing a key with different bytes fails. Logs support stream, byte cursor, and limit, with base64 preserving byte slices across UTF-8 boundaries. `/healthz` is liveness, not scheduler readiness. Worker lease/dispatch/report endpoints are under `/internal/v1` and require a distinct scoped worker credential.

## Compute reservations and recovery

`approval_required`, `budgeted`, and `mixed` modes share one ledger. Budgeted jobs run automatically within the configured allowlists, resource envelope, expiry, active-job limit, and remaining CPU/GPU allowance. Mixed mode routes jobs outside the automatic envelope to human review while retaining the overall allowance check. Approval mode always waits for a decision. There is no unbounded or free human override of the project allowance.

Admission atomically checks the project's lifetime `max_total_jobs` and simultaneous-job cap and reserves effective CPUs/GPUs multiplied by maximum charge duration. A submission slot counts each durable authorized execution attempt, including failed or cancelled attempts; review rejection and recovery of the same attempt do not consume additional slots. Adding or rotating keys never resets it. Policies predating this field use a finite default of 1,000; the portal supports explicit limits from zero to 1,000,000. Preparing revisions and waiting for review do not contact Slurm. The total cap is separate from request-rate limiting, which remains future work.

Maximum charge duration is requested walltime plus explicitly configured termination, overtime, and cleanup margins. The scheduler walltime is whole minutes. Allocation rounding must be bounded by the trusted site profile. Unexpected observed overage is charged in full, recorded as debt, and pauses new admissions. Accounting uses Slurm-reported allocated resource seconds, with a minimum one-second charge for an allocation. Its whole-second meter is not subsecond physical-time measurement, utilization, GPU memory, or site currency/billing weights.

The worker must obtain online dispatch confirmation before remote submission, then writes its own durable intent before contacting the backend. The remote helper also locks and journals each attempt. A saved receipt can be replayed after a report failure without resubmitting. An uncertain outcome reconciles the same attempt, keeps its funds reserved, and never blindly retries or infers non-submission from a missing queue row. Terminal jobs await usable final accounting before settlement. Output collection or storage failure is reported separately and does not suppress valid final accounting. Cancellation acknowledgement is not a refund. New admissions check current policy; dispatch rechecks revocation and policy. SQLite uses immediate transactions; PostgreSQL uses explicit row locks with concurrency regressions for admissions, leases, cancellation, authority changes and storage reservations.

This is conservative recovery, not a claim of distributed exactly-once execution. Offline database transfer and verification are implemented; automated disaster recovery, operator resolution of persistent ambiguous attempts, journal recovery after loss, queued-expiry automation, policy-version administration, and reconciliation after site accounting corrections still need protected-production acceptance. Per-attempt observations currently back off by two seconds, ten when uncertain/awaiting accounting; large clusters need batched adaptive polling.

## Slurm and environments

The remote side needs only a fixed Python 3 interpreter and existing Slurm CLI tools, not an installed broker daemon or REST API. The first adapter supports single-node jobs, explicit partition/account/QoS allowlists, Python or Bash entrypoints, arguments quoted by the broker, bounded logs, and declared output paths. Arrays, automatic requeue, arbitrary sbatch directives, multi-node allocation, exclusive jobs, job dependencies, GPU shards, and arbitrary environment installation are unsupported. Uploaded code executes only on the compute side; fake mode never executes it.

Each cluster's Environments section creates immutable recipes for existing system interpreters, Environment Modules/Lmod, Conda prefixes, Python virtual environments and Poetry-managed virtual environments. Recipe permissions in projects are grouped by cluster, and execution validates the exact recipe ID, revision and cluster. Module initialization, exact loads and optional search paths appear in the reviewed wrapper. Conda runs only inside the compute job; virtual environments use their interpreter directly without activation/project hooks. Discovery is bounded login-node metadata: it never executes candidate Python interpreters or Poetry projects, never scans an entire home directory, and queries Conda only at an explicitly configured location. Root-owned module trees are checked before availability discovery. An explicit metadata-only module mode lists provisional file names from operator-configured resolved roots without evaluating module code. Candidates remain unverified on compute nodes. Recipe hashes do not attest every remote executable/library.

The optional, default-disabled `environments` extension adds [reusable environment builds](environment-builds.md). Its implemented builder creates a Python venv from an empty standard-library closure or uploaded pinned, hashed wheels, entirely as an ordinary approved/budgeted compute job. A human first enables a narrow project policy and later explicitly permits a verified published environment revision for jobs. Compute, active-job and lifetime submission limits apply. Worker-side finalization reads bounded metadata without executing the new interpreter. Fake builds never publish; real builds require successful final accounting and matching receipts. Current per-object upload limits block large wheels. Conda/micromamba/Poetry provisioning, packed/node-local environments, automatic cleanup and content attestation remain future work.

The optional [workflow app](workflows.md) orchestrates at most 32 ordinary jobs
through success-only dependencies and retained artifact hashes. Each node has
its own review/admission and consumes normal compute and submission limits.
Artifact edges currently stay on one cluster; cross-cluster data movement,
native scheduler dependencies and [native arrays](arrays-design.md) remain
unsupported. Disabling the app does not stop core settlement of submitted jobs.

The optional [storage app](extensions-storage.md) provides shared project byte
and file budgets, per-cluster placement preferences and dry-run peak estimates.
These are advisory. Explicit operator-enabled probes use fixed read-only
directory metadata and filesystem capacity calls, with no recursive scans,
content reads or writes. Cached observations carry timestamps and unknowns;
native quota remains unknown unless a supported protected probe reports it.
This app does not redirect job writes, enforce OS quotas or migrate data.

Discovery saves partitions, node CPU/memory/GRES advertisements, own accounting associations when allowed, Slurm version, and relevant scheduler configuration. It does not equate visible resources with authorization or sum overlapping partitions as separate physical capacity. Finite termination/overtime candidates require operator verification, and cleanup bounds cannot be inferred reliably from ordinary discovery. Budgeted admission must wait for an explicit verified site profile. CPU/GPU resources are separate accounting dimensions.

SSH uses a fixed trusted configuration, strict pinned host-key validation, batch mode, no forwarding, and no agent-supplied host/command/key path. Browser cluster enrollment selects an alias advertised by an explicitly owned worker and stays within its exact hosts/users/ports and staging-root prefixes. Discovery is a leased worker operation. Optional explicit identity paths disable ambient agent use. MFA mode can reuse only a separately established protected ControlMaster socket and fails closed when the master is gone; the interactive Cisco login/reconnect flow has not yet been accepted against the university cluster.

Keep protected credential profiles and the deployed helper version unchanged until all their attempts have finished accounting. A new profile alias and connection provide a separate revision for changed settings. Read-only discovery may refresh an unchanged connection while jobs run, but cannot replace its execution revision while attempts remain unsettled. After settlement, authorized rediscovery can renew the derived binding to a changed protected profile; old prepared packages then require a new revision.

The protected site flag `automatic_execution_allowed: false` forces review during supervised site acceptance, regardless of automatic project/key settings. It is checked at admission and dispatch. Its absence retains earlier configured behavior. Supervised acceptance may record explicitly provisional timing assumptions; these are not evidence of finite worst-case cleanup and must not enable unattended spending. A human can explicitly accept the same-account development limitation for a supervised test without making the installation a protected deployment. The current test artifact path still relays bounded bytes through control; configurable direct delivery is documented as future work in the architecture.

## Current limits and release gates

Public JSON bodies are capped at 2 MiB; an individual upload at 512 KiB. Internal result reports allow 16 MiB for up to 10 MiB of raw declared artifacts plus bounded logs. Discovery snapshots are capped at 1 MiB. Storage reserves project and global bytes before writing; defaults are 64 MiB per project, 256 MiB globally, 1,000 objects per project, and eight in-flight uploads. Crashes retain reservations conservatively. Automatic orphan/reservation garbage collection, project retention/deletion and metadata/rate quotas are not implemented; operators must monitor development instances.

PostgreSQL concurrency and synthetic TLS directory acceptance do not establish hosted production readiness. Protected-installation secret isolation, institutional directory policy, institutional MFA, GPU runtime and other scheduler backends still need acceptance; OIDC is not implemented. A hostile payload sharing the remote account may alter same-user files/jobs or submit nested jobs outside the broker ledger. Hard ceilings across all such activity need cluster-side restrictions. Docker is a packaging and test boundary; giving the agent Docker-daemon access defeats same-host secret protection. Routine acceptance uses synthetic fixture credentials; a supervised site test is a separately authorized exception, not a protected deployment.

The repository is initialized locally and includes contributor/security guidance, migrations, schemas, pinned Python dependencies, default credential-free tests, and CI definitions. Publication, licensing, security contact setup, base-image pinning, vulnerability/license review, SBOM/signatures, and the broader release gates remain explicit maintainer work.
