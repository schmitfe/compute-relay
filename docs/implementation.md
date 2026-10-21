# Implementation plan and repository structure

## Delivery scope

Build a protected, local-first compute broker with Slurm over SSH, immutable job review, project-scoped agent credentials, durable execution and transactional compute allowances. The same domain model supports a shared server with organizational login. The initial supported protection boundary is a Linux service identity inaccessible to an ordinary desktop agent.

The repository now implements a development alpha spanning control, immutable review, reservations, workers, Slurm/SSH, fake execution, and MCP. See [current runtime scope](runtime.md) for precise limits and [container evidence](container-testing.md) for live test results. The source tree below remains the larger target layout. Milestones are ordered by dependencies and acceptance gates; protected installation and several later features remain incomplete.

## Prioritized follow-up: simpler agent onboarding

Recorded on 21 October 2026. **Pending implementation.** A new session should
open a configured science project and discover its authorized compute context
without receiving a project UUID, credential contents, or a manual tool list
in the prompt. These changes apply to core onboarding and must not require
storage, environment-build, workflow, or transfer extensions.

### ONB-1 — Infer the project from the API key

- Remove mandatory `BROKER_PROJECT_ID` from MCP launch configuration. A project
  key already identifies exactly one project through server-side authentication.
- Provide an authenticated context lookup; let the adapter obtain the project
  identity and insert it into project-scoped routes and job requests internally.
- Keep the broker authoritative. A supplied project ID cannot expand a key's
  authority. During migration, an optional legacy ID must match the authenticated
  project or fail clearly. Preserve opaque keys and hash-only server storage.
- Update the configuration generator, examples, and onboarding documentation.
- Acceptance: a fresh client with only endpoint/profile and token-file reference
  can discover targets, prepare a job, request execution, and retrieve results.
  Invalid, expired, revoked, and cross-project credentials remain denied; a
  conflicting legacy project ID fails without admitting work.

### ONB-2 — Configure the broker address once

- Treat the broker URL as MCP adapter connection configuration. It is not
  information the model must repeat in prompts or supply on individual calls.
- Support a reusable installation/connection profile containing the endpoint;
  science projects select that profile and their own credential-file reference.
  Preserve an explicit endpoint override for local and hosted installations.
- Generate the appropriate client configuration from the portal or setup tool
  so users do not manually copy internal deployment details into every project.
- Keep endpoint selection explicit and bound to the intended credential. Do not
  discover an arbitrary server and forward a key to it, embed secrets in URLs,
  or weaken the current HTTPS and redirect restrictions.
- Acceptance: subsequent sessions and projects can reuse a configured connection;
  profile/override precedence is deterministic, credential values stay out of
  generated configuration and logs, and existing explicit URLs remain supported.

### ONB-3 — Expose a complete authorized context through MCP

- Add a versioned read-only context tool/resource with the project name/identity,
  key scopes and execution-confirmation requirements, effective project and site
  resource limits, remaining compute/submission allowance, and permitted targets
  and environment revisions. Include enabled/usable extension capabilities.
- Supply the job-request schema and a valid request example through MCP so a
  fresh agent need not inspect broker source or guess an opaque request object.
- Preserve timestamps and uncertainty for saved inventory. Do not describe
  advertised capacity as current availability or a linked environment as usable
  when its revision is outside the grant. Never expose worker/SSH credentials,
  private authentication paths, or another project's data.
- Include concise local/hybrid/cluster planning guidance. Local hardware and
  available-memory inspection remain agent-local operations; local execution is
  outside the broker's compute ledger. Context discovery does not automatically
  route jobs, grant permissions, enable extensions, or authorize execution.
- Acceptance: fresh Codex and Claude Code sessions can learn the tools, request
  format, constraints and execution choices without repository-source access or
  a manually maintained onboarding brief. Test scope filtering, current policy
  changes, disabled extensions, stale/unknown inventory, and key revocation.

## Selected implementation stack

| Area | Choice | Reason and boundary |
| --- | --- | --- |
| Language | Python 3.12 and 3.13 initially | Shared control/worker/client ecosystem; explicit version matrix |
| Control service | Supported Django release line, initially evaluate 5.2 LTS, plus Django REST framework | Users, sessions, migrations, ORM and explicit domain services |
| Review UI | Server-rendered Django templates, progressive enhancement only where needed | Exact text/diff review without a separate frontend build system |
| State | SQLite locally; PostgreSQL on shared server | Backend-specific transactional ledger tests are mandatory |
| Work dispatch | SQL outbox + leased work items through a private worker API | Durable jobs without Redis/Celery in the first installation |
| MCP | Official Python MCP SDK; stdio facade first | Agent client has no privileged runtime access; HTTP/OAuth later |
| SSH | System OpenSSH plus SFTP and an audited one-shot remote Python helper | Protected MFA/session reuse; no cluster REST or persistent daemon prerequisite |
| Artifacts | Protected content-addressed filesystem with storage interface | No mandatory object-storage service; optional S3 later |
| Packaging | Python distribution with client/control/worker extras and Docker images/Compose | Separate authorities and independently minimal images |
| Runtime tests | pytest, Hypothesis, Django transaction tests, disposable SSH/Slurm, browser tests | Add with implementation; current tests use unittest + jsonschema |

Resolve exact compatible dependency versions and commit a lockfile in M1. Do not fabricate a lockfile for software not yet selected. Pin release images and CI actions, track dependency licenses and generate SBOM/provenance before publication. Avoid implementing a custom OAuth authorization server; use an established provider for hosted MCP when that milestone begins.

## Target source tree

```text
README.md
CONTRIBUTING.md
SECURITY.md
LICENSE                         # owner chooses before public release
pyproject.toml                  # introduced with runtime packaging in M1
uv.lock                         # resolved with actual runtime dependencies
contracts/                      # versioned planning/wire schemas and examples
docs/
  architecture.md
  api.md
  security.md
  backends.md
  deployment.md
  implementation.md
  testing.md
  adr/
  references/
src/compute_broker/
  domain/                       # typed immutable requests, policy, state transitions
  control/
    settings/                   # strict development/personal/server settings
    apps/
      identity/                 # local owner, external identity, memberships
      projects/                 # keys, scoped permissions, project policy
      clusters/                 # definitions, immutable profiles/connections
      environments/             # pinned named recipes and build provenance
      jobs/                     # revisions, decisions, attempts, outbox
      budgets/                  # grants, reservations, immutable ledger
      artifacts/                # authorized content-addressed object access
      audit/                    # sanitized durable events
    api/                        # DRF serializers/views and explicit service calls
    templates/                  # review, diff, comments, usage and connection state
  worker/
    client.py                   # private control protocol, leases and reports
    runner.py                   # typed operation execution and recovery
    journal.py                  # durable nonce/submission intent records
  backends/
    protocol.py
    fake.py
    slurm/
      discovery.py
      prepare.py
      submission.py
      status.py
      accounting.py
      models.py
  transports/openssh.py
  remote/helper.py              # reviewed finite operations, no arbitrary exec RPC
  storage/filesystem.py
  mcp/facade.py
  cli.py
tests/
  contracts/                    # executable now
  unit/                         # implementation milestones add remaining folders
  property/
  api/
  database/
  ssh/
  slurm/
  browser/
  deployment/
  acceptance/                   # explicit operator invocation only
  fixtures/slurm/<version>/
deploy/
  Dockerfile.control
  Dockerfile.worker
  compose.personal.yml
  compose.server.yml
  install/                      # protected directories/service identities/bootstrap
examples/                       # synthetic, never autoloaded real credentials
tools/
.github/workflows/
```

Domain services own authorization and state changes. REST, web views and MCP all call the same operations; a view must not bypass policy through raw model updates. Keep budget logic independent of Slurm parsers. A backend cannot call the ledger directly, and a worker cannot import a privileged database configuration.

## Milestone M0 architecture and contracts

Current deliverables are the architecture, threat model, API/data model, staged implementation/testing plans, upstream assessments, decision records and planning schemas/examples. Contract tests reject unbounded/multi-node requests, credential/approval overrides, native scheduler flags, unsafe paths, insufficient reservations, missing connection bindings and inexact wire counters.

Gate: all planning schemas/examples validate offline; negative cases pass; documentation links resolve locally; no runtime or isolation guarantee is reported as implemented. Git initialization depends on writable Git metadata; all planned content remains ordinary versionable files even before initialization.

## Milestone M1 control foundation and fake backend

| Work package | Concrete changes | Acceptance |
| --- | --- | --- |
| M1.1 package and settings | Create distribution/CLI, optional extras, strict config loader, migrations and development fake profile; lock dependencies | Clean install and fake startup on both Python versions; no real SSH auto-discovery |
| M1.2 identity and projects | Local owner enrollment, roles, scoped 256-bit agent tokens with hash/revocation, per-project query filtering | Auth matrix, cross-project object creation/list/download denial; no agent-admin authority |
| M1.3 immutable packages | Individual uploads, hashes, safe object store, transactional project/global storage and object quotas, request schemas, vetted RFC8785 library, immutable preparation and connection/environment revisions | Hash/size verification, concurrent/aborted streams and orphan cleanup, duplicate JSON/surrogate rejection, deterministic cross-language vectors, traversal and collision tests |
| M1.4 workflow and outbox | Job revisions, decisions/comments, single-attempt approval consumption, grant/reservation/ledger baseline on the fake backend, attempt lifecycle, idempotency records and leased fake work | Race duplicate execution requests/approval consumption and SQLite admissions; kill/restart at transaction boundaries; unknown side-effect state never blindly retries |
| M1.5 UI and MCP facade | Review exact files/diff, reject/revise/approve, status/results; stdio tools call authenticated API | Browser review matches submitted fake bytes; project key cannot approve; facade has no DB/SSH secrets |
| M1.6 fake Compose demonstration | Build minimal images; generate synthetic identities and local fake jobs | Image contents and startup tests; clearly labeled development profile |

This milestone makes the complete workflow observable without cluster credentials. It is the first reviewable application increment. It does not claim same-device secret isolation from a desktop agent.

## Milestone M2 protected installation and Slurm review mode

| Work package | Concrete changes | Acceptance |
| --- | --- | --- |
| M2.1 protected Linux service | Installer, separate identities, admin-owned images/config, private mounts/sockets, trusted enrollment and approval channel | VM tests from the agent UID deny secret reads, ptrace/process access as applicable, Docker control, config modification and approval escalation |
| M2.2 upstream reuse spike | Audit pinned HPC-MCP modules/tests against our interface; compare adapting selected transport/parsers to fresh thin code | Record license/attribution, retained/rewritten modules and conformance failures; no direct privileged upstream MCP endpoint |
| M2.3 OpenSSH and helper | Trusted host/user/config, pinned host keys, fixed helper operations, safe SFTP staging, bounded commands/results | Real disposable SSH tests cover remote-shell injection, symlink races, timeouts and environment sanitation |
| M2.4 Slurm lifecycle | Versioned discovery, exact script rendering, `sbatch --parsable`, durable UUID correlation, native status/cancel/accounting | Disposable real Slurm tests plus synthetic output fixtures; response-loss quarantine and native adoption proven |
| M2.5 university MFA | Trusted authentication helper, protected master/socket, ready/login-required lifecycle, no-fallback regular calls | Expire master between check and execution; no new unattended authentication/push; other cluster remains operational |
| M2.6 opt-in site review workflow | Internal then university tiny Python job, reject with comment, revise, approve, output retrieval | Explicit operator account/partition/cap; sanitized site record; no real job submitted by ordinary tests |

Slurm review mode still enforces hard request caps and reserves the finite reviewed envelope using the M1 ledger baseline. Establish a conservative finite site envelope before enabling version-one real submissions; unknown/unbounded partitions remain discovery/status-only. M2 may settle conservatively against the reserved maximum after terminal confirmation; M3 completes actual-usage settlement and the full two-database/concurrency/site tests needed for unattended spending. The exact university flow may require an approved institutional alternative if protected connection reuse is not permitted.

## Milestone M3 project compute allowances

| Work package | Concrete changes | Acceptance |
| --- | --- | --- |
| M3.1 ledger completion | Extend the M1 baseline with actual Slurm usage settlement, idempotent adjustments, debt and full reconstruction | Reference-model/property tests; no floating-point or double settlement |
| M3.2 atomic admission | SQLite IMMEDIATE and PostgreSQL row-lock paths, active-job cap, policy/credential/pause checks with outbox commit | Independent-process races on both actual databases; cross-dimension failure rolls back all changes |
| M3.3 site charge model | Effective allocated cores/devices and verified finite termination/cleanup margin; whole-minute scheduler limits | Per-site evidence; prepared envelope checked against automatic and hard caps; unknown/unbounded sites cannot auto-admit |
| M3.4 accounting and recovery | Final `sacct` normalization, no step double-counting, accounting-pending and uncertain reservations, requeue/overage quarantine | Failed/cancelled jobs charged; outages cannot refund uncertainty; full observed debt retained |
| M3.5 mixed mode and stop | Automatic envelope plus review fallback, grant expiry, explicit pause/cancel, key rotation | All keys share balance; expired grants do not erase commitments; dispatch race is documented/tested |

Initial usage units are CPU core-seconds and GPU device-seconds. A grant restricts supported GPU types rather than claiming all devices have equal cost. Site allocation granularity and termination overtime are part of the bound; the request's `--time` alone is insufficient. Same-UID nested submissions remain outside the service guarantee.

## Milestone M4 software environments and storage-aware workflows

**Planned extension, not current runtime support.** Existing environments are immutable cluster-bound recipes for pre-existing installations. Implement storage planning first, reusable builds next, broker-managed DAGs after artifact references, and native arrays only after task-level admission/accounting. The [architecture extension](architecture.md#planned-project-storage-guidance-and-placement) specifies lifecycle, placement and integrity boundaries.

| Work package | Concrete implementation | Required checks |
| --- | --- | --- |
| M4.1 targets and advisory budgets | Migrate `StoragePool`, `StorageTarget`, `ProjectStoragePolicy` and timestamped observations; add project byte/inode guidance and result/environment/work/scratch selectors beneath protected connection roots | Two targets on one pool do not invent separate capacity; unknown quota/inodes remain unknown; foreign targets and arbitrary path overrides are rejected |
| M4.2 pressure observations | Fixed bounded worker metadata/quota operations, expected mount identity, optional low-space admission guard, observation age and recovery hysteresis | Missing mounts, stale/failed probes, byte/inode pressure, dispatch race and safe recovery; accounting/observation still work during pressure |
| M4.3 artifact locations and transport | Add remote `ArtifactReplica`, streaming/resumable verified blobs and permitted direct/relay staging; preserve current small-package limits for ordinary code | No whole-wheelhouse JSON/base64 in memory; quota reservation, resume/hash mismatch, blocked direct route, explicit fallback and cross-project isolation |
| M4.4 environment requests | Immutable `EnvironmentSpec` with allowed base/manager, exact lock/package hashes, representation and compute/peak-storage bounds; new explicit build/use policy and scopes | New scopes do not appear on existing keys automatically; successful build cannot silently broaden project environment grants; malformed package options/URLs/includes rejected |
| M4.5 venv build/publish | Pure build renderer; ordinary charged Job/Attempt; final unique prefix from creation; compute-side validation; bounded metadata-only finalization publishes a new Environment revision | No package/interpreter code in control/worker/login; failed/uncertain attempts never publish; two clients reuse one ready generation; generated console scripts work at the published prefix |
| M4.6 manager plugins | Conda/micromamba explicit locked packages and Poetry lockfile with selected interpreter; isolated public-package acquisition, explicit online/offline behavior | Same cluster/platform binding; pinned installer/tool versions; incompatible locks and changed package bytes fail; no implicit base-environment installation |
| M4.7 packed runtimes and retention | Conda-pack archive to scratch and optional read-only Apptainer SIF after site capability checks; per-node cache pins, migration receipts and bounded GC | Archive escape/expanded-size/inode attacks; no shared writable image across tasks; scratch exhaustion has no home fallback; pinned generations survive GC/migration |
| M4.8 artifact inputs and static DAG | WorkflowRevision/Run/NodeRun, success-only edges and pinned producer outputs; leased dispatcher prepares/admit ordinary jobs only when concrete inputs exist | Cycles, bounded fan-out, missing outputs, changed digests, double activation, restart, stale credentials, budget pause and explicit retries |
| M4.9 native arrays | Typed index set/concurrency; `Attempt.admission_units` defaults to one for history; indexed observations; one parent journal with aggregate reservation and final per-element charge sum | Every task counts toward lifetime/active caps; no aggregate/step double charge; partial cancel, missing accounting and lost submission replies retain uncertainty; subset retries are new admissions |
| M4.10 workflow review contract | Initially retain each concrete package's approval; separately specify optional sealed workflow-template authorization and transferred aggregate holds | No existing exact-manifest approval authorizes new child code or unknown bindings; graph edits/retries/expansion cannot evade key confirmation or project limits |

Suggested future API surface is below. These routes and scopes are proposals and must not be advertised by current MCP tools until implemented and tested.

| Proposed API | Purpose |
| --- | --- |
| `GET /api/v1/projects/{id}/storage` | Budgets, permitted targets, retained/pending estimates, native quota and pool pressure with freshness/uncertainty |
| `POST /api/v1/projects/{id}/storage-plans` | Read-only placement/peak-space planning; optional advisory hold has explicit expiry and ownership |
| `POST /api/v1/projects/{id}/environment-specs` | Seal a validated base/manager/lock/storage request under explicit environment-build policy |
| `POST /api/v1/environment-specs/{id}/builds` | Create an ordinary reviewable build job; execution remains an explicit execution request |
| `GET /api/v1/environment-builds/{id}` | Build attempt, logs, receipts, published recipe and reuse/retirement state |
| `POST /api/v1/projects/{id}/workflow-revisions` | Seal a bounded graph and artifact edges; execution creates a separate durable run |
| `GET /api/v1/workflow-runs/{id}` | Node/task progress, blocked inputs, storage/compute estimates and retry lineage |

Source additions should keep pure validation/planning/rendering separate from database services and fixed worker metadata operations. Start with `domain/storage.py`, `domain/environment_builds.py` and `domain/workflows.py`, then control models/services/forms and backend renderers with tested manager strategies. Do not expose arbitrary shell, installer options, path-based copy/delete or expression evaluation as an agent tool. Larger package/environment bytes require the transport work above: current 512 KiB input files and 64 MiB project broker storage are intentionally too small for general scientific wheelhouses.

No installer/build hook runs in a credential-bearing service. Builds, resolution that executes package hooks, validation, unpacking and scheduled migration consume ordinary compute allowance and submission slots. Cache keys bind project/connection/remote identity, base recipe, interpreter/platform/ABI, manager/builder version and dependency/package hashes. Build a directory at its final unique prefix and publish a reference, rather than relocating a completed venv. Same-UID managed directories and image files are not hostile immutable storage; sealed definitions and provenance do not change filesystem ownership. Private package credentials require a separate acquisition design before support is advertised.

Gate: hostile dependency/build input cannot access broker keys/runtime sockets; changes require new review or policy admission; failed builds charge allocated usage and never poison a valid cache. A functional GPU environment is tested on an expressly permitted GPU, not inferred from inventory.

## Milestone M5 hosted multi-user and remote clients

Implement PostgreSQL operations/migrations, organizational OIDC with stable identity linking, group mapping/deprovisioning, per-user cluster connections and optionally outbound personal connectors. Keep one authoritative ledger. Add standard HTTP MCP OAuth discovery/audience/scopes using a supported identity provider and test each intended hosted client. Configure server TLS, backup/restore, retention, metrics and request limits.

Gate: cross-user/project/job/artifact isolation and revoked membership tests; no LDAP-to-SSH impersonation; personal-to-server migration fences old admissions and reconciles native jobs; no offline connector spending. The service UI's account model alone does not create hostile isolation for users sharing a remote Unix UID.

## Milestone M6 public release and backend extensions

Before a public alpha, choose ownership/name/license, configure private security reporting, publish accurate support/limitation documentation, audit copied code and dependencies, pin builds/images, scan source/history/build contexts, and run clean protected installation/upgrade/restore/rotation tests. Use protected publication environments for explicit maintainer release approval. Runtime conformance, real site acceptance and OS isolation evidence must be separately reported.

After the local Slurm workflow is reliable, evaluate FirecREST and Slurm REST adapters under the same protocol. HTCondor and Kubernetes need separate capability/allocation/accounting semantics and conformance tests; do not implement stubs that silently accept Slurm options. Native arrays follow M4's explicit task-accounting gate. RASER-style long-lived agent runtimes, work stealing and checkpoint/requeue remain later workload/runtime features, with separate retry/credit/side-effect tests. See the [RASER assessment](references/paper-2609-03598.md).

## Issue and pull request conventions

Each work package becomes an issue with problem/behavior, affected trust boundary, acceptance test IDs, migrations, dependency and documentation changes. Name implementation PRs after final behavior; include exact validation and remaining site-dependent gates. Security-related fixes include a regression case. A backend extension includes capability declarations and parser fixtures with producing command/version and sanitized provenance.

Protected main is the target branch. Require review and relevant CI before merge; keep public fork checks credential-free and institutional acceptance outside PR CI. Do not add automatically scheduled compute tests or publication automations without explicit authorization.

## Decisions still requiring site or owner input

| Decision | Default in this plan | When needed |
| --- | --- | --- |
| Public name, rights holder and license | Provisional Compute Broker; no license assigned | Before accepting external code for distribution/public release |
| University MFA/reuse rules | Human authentication in protected connector, fail closed | M2 site acceptance |
| Trusted human approval device/session | Independent channel for strict mode | Protected bootstrap |
| Exact Slurm versions/accounting fields and margins | Discovered privately, unsupported until verified | M2 fixtures and M3 admission |
| Shared remote UID acceptance | Documented API-only isolation; no hostile tenant claim | Before multi-user deployment |
| Organizational IdP and LDAP mapping | Existing OIDC provider preferred | M5 |
| Protected OS support beyond Linux | Deferred | Before advertising additional platforms |

These decisions do not block architecture or fake-backend development. They are gates for the specific capabilities they affect.
