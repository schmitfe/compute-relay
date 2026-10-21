# Optional module boundaries

The core must remain usable for a small reviewed or budgeted job when every
optional module is disabled. An optional module adds a separately registered
Django application, routes, scopes, views, and MCP tools. It does not create a
second admission ledger or a second path to execute a workload. Modules can be
separated into distributions later; Git submodules are not required.

| Responsibility | Core | Optional module |
| --- | --- | --- |
| Identity and authority | Human sessions, project keys, cluster/environment allowlists, worker identity | Additional explicit scopes and project policy that narrow authority |
| Jobs and money | Immutable preparation, exact human decisions, reservations, lifetime/active caps, dispatch fencing, reconciliation and settlement | Concrete jobs submitted through those same transitions |
| Files | Bounded source uploads, retained job artifacts, quota reservations | Persistent storage inventories and data plans beyond a single job |
| Environments | Select an approved cluster-bound recipe; saved software discovery | Build, verify, and promote reusable managed environments |
| Scheduling | One bounded execution attempt at a time | Finite workflows, artifact dependencies and bounded fan-out |
| Execution worker | Protected credentials, journal, backend protocol and HTTP client; no controller database | Explicitly enabled worker implementation and bounded metadata |
| Agent interface | Project-scoped core MCP tools and capability discovery | Separately registered tools enabled explicitly in the client |

The core `Environment` recipe model and per-job `Blob`/`Artifact` models remain
available without the managed-environment or persistent-storage modules. Their
names do not imply those optional modules are installed.

## Enablement and disabling

Production settings treat an absent or empty `BROKER_EXTENSIONS` as no optional
modules. Controller settings install only selected applications and include only
their routes. The MCP process separately registers only its selected modules;
client registration does not grant server authority. A worker separately
advertises modules from its protected configuration. Unknown names and duplicate
names fail configuration validation; agent input must never select an import
path.

A core-only fresh database has no optional application models, migration history
or tables. Disabling a module on an existing installation does **not** delete its
tables or historical data. It prevents new work from that module; existing native
jobs still need core observation, cancellation, and accounting until their
reservations settle. Re-enabling or uninstalling a module needs its own migration
and operational plan.

The test settings automatically enable available optional applications for their
own suites. That convenience must not change production defaults. Boundary
acceptance therefore starts separate Python processes with production settings,
both without the variable and with an explicitly empty value.

## Review checklist for a new or changed module

- [ ] Keep module models, migrations, API routes, templates and MCP registration
  inside the module. Core imports its registry and lifecycle interface, never a
  module's models or views just to start the server.
- [ ] Declare the operator-selected registry name and explicit scopes. Check
  project ownership and effective key/cluster restrictions on every read and
  mutation, including retained artifact references. An enabled module is not an
  authorization grant.
- [ ] Validate finite input sizes, cardinalities and schemas before creating
  durable work. Freeze input references and bind module context into the prepared
  package. Reject changed or stale revisions at admission and dispatch.
- [ ] Delegate workload execution to ordinary core jobs. Admission must reserve
  funds, consume any required human decision and lifetime slot exactly once.
  Fan-out must never multiply work outside the core ledger or raise project caps.
- [ ] Use admission and dispatch hooks to enforce additional constraints. Define
  what happens when an originating key is revoked, a worker loses support, the
  module is disabled, or its policy changes between preparation and dispatch.
- [ ] Keep worker implementations independent of Django, controller models, the
  controller database and human credentials. Resolve credentials only through
  protected worker configuration; do not expose them in leases or MCP results.
- [ ] Treat background maintenance as a separate bounded operation with explicit
  identity, generation, limits and retry semantics. A maintenance operation must
  not become an unmetered alternative for remote compute. Preserve the core lease
  fields and the worker's explicit module allowlist.
- [ ] Keep remote operations and generated-file writes outside metadata locks.
  Use durable claims and idempotent recovery for crashes between preparation,
  admission, submission and report processing. Test real SQLite and PostgreSQL
  concurrency where the transition depends on their different locking behavior.
- [ ] Run post-report processing after core accounting commits. A module failure
  must not undo settlement or strand a completed job's funds. Replay durable
  reports safely; do not rely on a callback having run exactly once.
- [ ] Bound and fairly rotate background scans. A run awaiting approval, another
  worker's run, a revoked key or an unavailable module must not starve ready core
  observations or unrelated projects. Test more pending runs than the scan limit.
- [ ] Keep status reads free of workload admission. Refreshing derived status is
  different from starting another attempt. Cover cancellation during preparation,
  accounting pending after native completion and terminal failure propagation.
- [ ] Test disabled, controller-only, worker-only and client-only enablement,
  missing dependencies, unsupported versions and disable/re-enable recovery.
  Preserve ordinary core jobs with all modules disabled.
- [ ] Document dependency versions, operational prerequisites, migrations,
  uninstall retention, capability gates and unverified assumptions. Publish no
  secrets, site paths or claims of security isolation established only by mocks.

## Executable acceptance and its limits

Run the focused checks from the checkout with the normal test dependencies:

```bash
python -m pytest tests/system/test_module_boundaries.py -q
```

The controller checks migrate a new disposable SQLite database while an import
guard rejects every registered optional module. They assert that optional models,
tables and representative routes are absent. Calls through the official MCP SDK
and facade use real authenticated Django HTTP handlers for upload, preparation,
budget admission and status. A synthetic worker simulates completion and produces
one settled CPU-core-second with no remaining reservation. The uploaded source
deliberately raises an exception if executed; the fake backend never executes it.

A separate process imports every worker implementation module with imports of
Django, controller code and optional modules forbidden, then polls an idle worker
and calls the ordinary MCP capability tool. This catches accidental runtime
coupling even when the full test process already has Django loaded.

These are default-configuration and dependency-boundary checks. Their in-process
HTTP bridge does not test a deployed server, stdio transport, SSH, real Slurm,
process isolation or secret protection. The existing official-SDK stdio test,
native HTTP smoke tests, disposable Slurm acceptance and protected-installation
security review supply separate evidence. Passing these checks is necessary for
modularity, not sufficient for production security or any module's correctness.
