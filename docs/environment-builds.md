# Cluster environments

The `environments` extension provides one implemented builder: a Python virtual environment created as an ordinary scheduled Slurm job. A recipe can contain an empty standard-library closure or uploaded wheels with exact distribution versions and SHA-256 hashes. Conda, micromamba and Poetry builders are rejected explicitly. Existing manually enrolled recipes for those managers remain core functionality; this extension does not install them.

The normal `compute-relay` launcher includes this extension and its migrations, personal connector support and MCP tools. Custom control/worker deployments can enable it with `BROKER_EXTENSIONS=environments` on control and `environments` in the protected worker's extension list. The worker advertises its supported extensions. Custom MCP adapters must enable the extension too. A control instance without the extension does not install its models, routes or tools. Disabling it blocks new admission and first dispatch; core monitoring, cancellation and settlement of dispatched jobs continue.

## Human policy and agent authority

A project administrator configures `/projects/{project_id}/environment-build-policy/` or the CSRF-protected session API `PUT /api/v1/projects/{project_id}/environment-build-policy`. Agent keys cannot change this policy. A policy selects existing cluster-bound bootstrap environments, resource limits, source type and a compute platform/Python target:

```json
{
  "enabled": true,
  "allowed_base_environment_ids": ["11111111-1111-4111-8111-111111111111"],
  "allowed_managers": ["venv"],
  "allowed_sources": ["uploaded_wheels"],
  "auto_permit_builds": true,
  "resource_limits": {"cpus": 1, "memory_mib": 1024, "time_limit_seconds": 600},
  "platform_tag": "linux-x86_64",
  "python_major_minor": "3.13",
  "max_environment_bytes": 1073741824,
  "max_environment_files": 20000,
  "minimum_free_bytes": 1073741824,
  "minimum_free_files": 10000
}
```

These are illustrative values. Pin the Python version and `sysconfig.get_platform()` observed on the intended compute platform; login-node discovery is not compute validation. Builds are CPU-only, single-node jobs, with whole-minute walltime. The venv builder accepts system Python or an approved module-provided Python bootstrap. Published venv recipes preserve the base module names, initializer and module paths, so module-provided runtime libraries remain available on reuse. Conda, Poetry and existing-venv bootstraps remain unsupported; selecting an existing environment for ordinary jobs does not enable a builder for its package manager. The bootstrap environment must already be permitted by ordinary project execution policy. A changed build policy, bootstrap binding or connection revision requires a fresh build and review.

Give an agent the optional `environments:build` scope together with `jobs:write`, `jobs:execute` and the read scopes needed to monitor its jobs. Uploads additionally require `objects:write`. A scope never overrides project review, compute allowance, total submission cap, active-job cap, current connection permissions or the protected worker's capabilities. Policy and scopes are checked again at admission and immediately before first dispatch. A human can reject a build on the ordinary job-review page and return a comment. `auto_permit_builds` defaults to false; enabling it authorizes verified builds and subsequent immutable updates for ordinary project jobs, subject to their normal admission rules.

## Build API

Build and inspection mutation requests use the ordinary `Idempotency-Key` header. Stage wheels through the resumable file helper before referencing their hashes. The inline object endpoint retains its 512 KiB limit; resumable uploads accept files up to 8 GiB, with a 64 GiB aggregate bundle, subject to project quotas. This release does not resolve package names against PyPI or fetch arbitrary URLs.

Create an empty closure with `POST /api/v1/projects/{project_id}/environment-builds`:

```json
{
  "name": "stdlib-analysis",
  "connection_id": "22222222-2222-4222-8222-222222222222",
  "base_environment": {
    "id": "11111111-1111-4111-8111-111111111111",
    "revision_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  },
  "manager": "venv",
  "requirements": [],
  "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 512, "time_limit_seconds": 60},
  "slurm": {"partition": "debug", "account": "broker"},
  "estimated_bytes": 10000000,
  "estimated_files": 100
}
```

A nonempty `requirements` entry has exactly `name`, `version`, `filename`, `sha256` and `size_bytes`. The wheel filename must match its exact name and version. At most 64 distinct wheels, 8 GiB per wheel and 64 GiB aggregate recipe wheel bytes are accepted, subject to project quotas. There are no editable sources, source distributions, environment hooks, dependency ranges or remote indexes. The caller supplies the full closure. Compute-side installation uses `--no-index --no-deps --only-binary=:all: --require-hashes`, followed by `pip check`; unresolved dependencies fail the build. Installation may still execute installed package behavior on the compute node. The built interpreter is never executed by control, the credential worker or the login-node metadata finalizer.

| Request | Result |
| --- | --- |
| `GET /api/v1/projects/{id}/environment-build-policy` | Current explicit policy and available builder |
| `GET /api/v1/environments/{id}/inventory` | Actual observed Python/packages/fingerprint/time and managed-receipt drift status |
| `POST /api/v1/environments/{id}/inspect` | Prepare and request a bounded scheduled inventory job; resume an existing pending probe |
| `POST /api/v1/environments/{id}/packages` | Prepare and request a new environment or immutable update from uploaded wheel descriptors |
| `GET /api/v1/projects/{id}/environment-builds` | Bounded project build list, filtered by key's cluster access |
| `POST /api/v1/projects/{id}/environment-builds` | Immutable normalized spec and a build, or a ready cache match |
| `POST /api/v1/environment-builds/{id}/prepare` with `{}` | Ordinary immutable Job package, including complete build script, recipe, lock and wheel descriptors |
| `POST /api/v1/environment-builds/{id}/execution-requests` with `{}` | Normal confirmation or budgeted admission; consumes an Attempt and ordinary lifetime submission slot when authorized |
| `GET /api/v1/environment-builds/{id}` | State, ordinary job/attempt links, receipt, estimates and published environment reference |
| `POST /api/v1/environment-builds/{id}/promote` | Human project-admin session and CSRF only; explicitly adds a ready environment revision to the project execution allowlist |

Optional MCP tools mirror policy read, inventory, inspection, package requests, create, prepare, execution request and status. They expose no policy update, environment promotion, package-manager command, shell endpoint, local-file read or SSH credential. `request_environment_packages(environment_id, packages, ...)` combines recipe creation, preparation and ordinary execution request. For an existing ready managed environment it retains the original approved bootstrap and pinned requirements, replacing only explicitly supplied package names. It records the parent environment reference and creates a fresh prefix/revision. A ready cache hit returns without submitting another job.

## Execution, publication and reuse

`EnvironmentSpec` pins project, cluster, base revision, target platform, exact lock closure, resources and storage estimate. `EnvironmentBuild` pins the policy revision/hash, creation key, ordinary Job and its single Attempt. Preparation sets `Job.extension_name=environments` before the package digest is generated. Calling the generic core job-execution API therefore invokes the same extension admission checks. A build admits only one Attempt; a failed or cancelled build needs a new build, not a reused installation directory.

The compute job creates its final prefix directly at `<protected remote_root>/<project UUID>/<job UUID>/<attempt UUID>/environment`, using copied interpreter files. It never builds elsewhere and renames the completed environment. Existing prefixes cause failure. Empty closures do not bootstrap pip. The job verifies the newly created interpreter on compute, counts files and logical bytes without following directory symlinks, and emits a bounded declared receipt with build/spec/attempt identity, target Python/platform, lock hash and timestamped measurements.

After a successful terminal observation, the extension's fixed remote operation reads metadata only. It verifies the broker's own attempt journal and exact native identity, service-derived work directory, declared recipe hash, bounded receipt, and no-follow regular-file metadata for `pyvenv.cfg` and the copied `bin/python`. Special files, symlinks and hardlinks in these checked paths fail closed. It does not import packages, run `pip`, execute the built interpreter or read SSH secrets. The control service publishes an immutable cluster/project-bound Environment only after usable final accounting has settled and matching finalization metadata passes the current policy checks.

Publishing makes the recipe visible in the project. With `auto_permit_builds=true`, a verified, complete runtime inventory and still-authorized creating key also permit the revision through the ordinary project allowlist and audit trail. Otherwise a human administrator explicitly promotes it. Normal compute approval and budget rules continue to apply. A fake backend marks the build `simulation_only`; it can never publish or promote an environment because it never ran the payload. Missing, failed, stale-generation, mismatched or uncertain receipts likewise cannot publish.

Compute inventory records the actual Python version, implementation, platform, distribution names/versions, full-inventory fingerprint and observation time. Registered labels remain descriptive. Existing recipes can be inspected through an ordinary metered job without enabling installation policy. Displayed inventories are bounded and explicitly mark truncation; automatic permission requires a complete managed build inventory. A later inspection compares its fingerprint with the immutable managed receipt and reports drift. This identifies version/package changes, not arbitrary modifications to package file contents. Python is the first supported language; Julia is not implemented.

Cache reuse is restricted to a ready environment under the same project, connection/base revision, normalized recipe/lock/platform, and unchanged build policy revision/hash. Failed or unknown prefixes are never cache hits. A cache hit refers to the original immutable prefix; it does not claim the files still exist, attest all their contents or share them across users/projects. The cluster account remains the filesystem trust boundary. Same-UID hostile code can alter same-user data in this development deployment.

Core `WorkerReport.extension_data` durably retains the finalization metadata before the publication hook runs. A failed post-commit hook does not undo accounting. `services.sync_from_report(report)` is an internal recovery function that retries publication from that saved generation without remote execution. Missing remote finalization cannot be converted into success by an agent; there is currently no user-facing remote revalidation or prefix repair operation.

## Storage, retention and current limits

Byte/file estimates, `statvfs` headroom checks and timestamped measured bytes/files are advisory planning information. They are not an operating-system quota, a concurrent storage reservation, exact allocated filesystem blocks, or a guarantee that a shared NAS cannot fill during installation. A build can consume space before its post-install cap check fails. Operators should set conservative policy limits and reserve headroom for institutional services outside this broker.

The implemented prefix is under the protected attempt staging root. It is not
routed through an enrolled environment storage target and does not use the
optional storage extension's reservations. Retain the prefix and trusted
bootstrap runtime for as long as jobs use the environment. There is no automatic
cleanup, repair or cross-root migration. Python venv prefixes generally cannot
be assumed relocatable. Other package managers need tested lock/source
validation before builds are enabled; see the [roadmap](roadmap.md).

The test suite verifies renderer purity, pinned wheel validation, ordinary approvals and budgets/caps, generic-route and dispatch fencing, explicit/automatic permission, conservative cache reuse, fake/failed/stale/mismatched publication denial, durable metadata recovery, typed MCP tools, and a synthetic metadata finalizer whose sentinel interpreter would reveal accidental execution. A separate MCP integration test runs the exact first-party inventory/build scripts in disposable compute directories, installs real fixture wheels, updates version 1 to version 2 while preserving version 1, and detects changed package metadata. It does not execute an installer inside control or a credential worker. Actual Slurm acceptance is a separate disposable-cluster test.

## Opt-in disposable Slurm acceptance

`tools/environment_build_smoke.py` reuses the existing bounded HTTP smoke client and requires both an explicit confirmation flag and synthetic `slurm-fixture` metadata. It creates a new review-only project/key, configures a build policy, builds an empty standard-library venv through ordinary human approval and worker execution, waits for settled native accounting and verified finalization, explicitly promotes the environment in a human session, and runs a second approved job using that prefix. It checks the result's hash and actual `sys.prefix`, conservative cache reuse, two lifetime submission slots, nonzero spent CPU time and zero outstanding reservation. Fake mode and nonfixture metadata are refused. No SSH key or worker token is read by this client.

From a checkout with the extension and generic extension key-scope metadata integrated, use a fresh disposable project name and volumes:

```sh
BROKER_TEST_PORT=18767 docker compose -p cb-environment-acceptance -f deploy/compose.test.yml -f deploy/compose.environments-test.yml up --build -d control worker
BROKER_TEST_PORT=18767 docker compose -p cb-environment-acceptance -f deploy/compose.test.yml -f deploy/compose.environments-test.yml run --rm smoke
BROKER_TEST_PORT=18767 docker compose -p cb-environment-acceptance -f deploy/compose.test.yml -f deploy/compose.environments-test.yml down -v
```

The overlay enables `environments` for the fixture and mounts the test client read-only. It passes `--module-bootstrap` to build from `fixture-python/1.0` and asserts that module remains loaded in the second job. The first command also starts the disposable Slurm dependency; all installer/interpreter execution remains on its compute job. This empty closure deliberately does not require pip bootstrap. The default pinned target matches the Debian Bookworm fixture (`linux-x86_64`, Python `3.11`); on another architecture, explicitly pass the fixture target options to the smoke client. The cleanup command applies only to that named synthetic Compose project. A successful unit test or Compose configuration check is not evidence that this native acceptance has run; record the actual smoke JSON and final accounting separately.

## Cluster recipes and inspection

Open **Clusters → your cluster → Environments → Add environment**. Recipes belong
to one cluster connection (including its SSH account), and projects explicitly
grant the recipes they may execute. Registering the same host under another
account therefore gives it its own observations and recipes.

1. Choose **Existing Python**, **Environment Modules / Lmod**, **Conda / Anaconda /
   Miniforge**, **Existing Poetry virtual environment**, or **Existing Python
   virtual environment**.
2. Select exact modules from that cluster's saved catalog, or enter exact names
   manually in load order. Selecting modules while using the default system
   recipe automatically selects Modules/Lmod and resolves the default interpreter
   through the loaded PATH. Conda, Poetry and venv recipes can retain these module
   loads, for example a Miniforge module or the compiler/Python providing runtime
   libraries. Module initialization stays optional and uses the cluster-provided
   command by default.
3. Keep the personal connector running and press **Inspect selection**. This is
   an explicit human action, with CSRF and current connection-revision checks.
   The updated connector must advertise `personal_environments_v1`; restart it
   after upgrading the package. Merely opening or saving the page does not run
   discovery commands.
4. Select **Detected Python** or **Detected environment** from the returned
   dropdowns. A Conda prefix fills its interpreter, prefix and Conda executable.
   Alternatively choose the manual option and enter existing absolute paths.
   Changing selected modules requires a new inspection before reusing detected
   paths. Saving creates an immutable recipe; grant it in project settings.

Inspection runs a fixed, bounded program through the existing authenticated SSH
session. It uses the remote login shell, loads only the selected modules, resolves
Python/Conda/Poetry paths, and uses executable/file metadata instead of executing
discovered Python interpreters. Where Conda is available it runs
`conda info --envs --json` with plugins/config disabled and an isolated HOME;
bounded metadata from the user's `.conda/environments.txt` also contributes
prefixes. Existing Poetry environments are observed beneath the standard
`~/.cache/pypoetry/virtualenvs` directory; custom Poetry locations can be entered
manually. It does not run Poetry in a project or install software on a login node.

Results carry a timestamp, belong to the requesting human and cluster revision,
and are **login-node observations, not compute-node validation**. A cluster test
or normal scheduled job verifies compute-node availability. Missing paths,
truncated results or unusual site setups can still be configured manually.
Conda activation hooks run later inside the authorized compute job, through
`conda run --prefix`; Poetry recipes use their existing venv interpreter directly.

### Agents requesting a new environment

The bundled optional environment-build extension already exposes typed MCP tools
for this workflow, identically to compatible OpenAI and Claude MCP clients:

1. A human grants the base recipe to the project, enables environment builds in
   its environment-build policy, and grants the agent key `environments:build`
   plus the normal job scopes. Source uploads also need `objects:write`.
2. The agent calls `get_environment_build_policy`. Its response includes the
   approved bootstrap recipe references, cluster connection IDs and configurations,
   supported managers, target Python/platform and resource/storage limits.
3. The agent checks `get_environment_inventory` for the actual Python version,
   package versions and observation time. `inspect_environment` schedules a
   bounded probe when this is missing or needs refreshing; it returns an existing
   pending probe when resumed. The registered environment name is descriptive.
   Omitted resources retain a pending probe's saved request. Sole configured
   partition, account and QOS choices are filled even when `slurm` supplies only
   some fields; choose ambiguous options from `list_compute_targets`. A failed
   preparation stays a draft. Resume it with `inspect_environment`, supplying
   corrected resources or scheduler options and a new idempotency key when the
   arguments change. Corrections create an immutable job revision before an
   execution request exists, retaining the original job in its history. Generated
   probe inputs cannot be revised through the ordinary `revise_job` upload path.
   Once execution is requested, retries reuse it; poll inventory or cancel the
   existing job before requesting different settings with a new key.
4. The agent can call `request_environment_packages` with uploaded pinned wheel
   descriptors. This creates, prepares and requests a normal build. For a managed
   environment, the same tool creates an updated immutable revision, preserving
   the original prefix and retaining pinned packages except names explicitly
   replaced. The complete dependency closure must be provided; no package-name
   resolver is included. Alternatively `create_environment_build` accepts
   a pinned `venv` recipe using
   an approved system or module-provided Python. It can request a standard-library
   environment or a hash-pinned uploaded-wheel closure.
5. For the granular recipe API, `prepare_environment_build` creates a normal reviewable job package.
   `request_environment_build` follows the project's approval/budget policy and
   consumes its ordinary compute allowance and submission slot. Installation and
   verification run in the scheduled compute job.
6. `get_environment_build` returns status and a ready environment reference after
   settled accounting and receipt verification. If the administrator enabled
   `auto_permit_builds`, a complete verified runtime inventory and current key
   authority allow ordinary jobs to use the revision automatically. Otherwise
   the human explicitly permits it. Compute review/budget rules still apply.

Built environments include their actual runtime inventory in the immutable
receipt. Later compute-node inspection can flag package/version drift from that
receipt. Python is the only supported build language. Other languages are
tracked in the [roadmap](roadmap.md).

A venv built from module Python retains the base module names, module paths and
optional initializer when reused. Retain its prefix and base runtime on that
cluster. Existing Conda/Poetry environments are selectable, but installation
builders are unsupported. See the [roadmap](roadmap.md) before proposing them.

The fixed Conda query follows the [official Conda info interface](https://docs.conda.io/projects/conda/en/stable/commands/info.html). The standard Poetry cache and prefix behavior follow [Poetry configuration](https://python-poetry.org/docs/configuration/).
