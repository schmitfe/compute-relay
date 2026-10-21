# Optional reusable environment builds

The `environments` extension provides one implemented builder: a Python virtual environment created as an ordinary scheduled Slurm job. A recipe can contain an empty standard-library closure or uploaded wheels with exact distribution versions and SHA-256 hashes. Conda, micromamba and Poetry builders are rejected explicitly. Existing manually enrolled recipes for those managers remain core functionality; this extension does not install them.

The extension is disabled by default. Enable it with `BROKER_EXTENSIONS=environments` on control, run Django migrations with that same setting, and enable `environments` in the protected worker's `extensions` list. The worker advertises its supported extensions. The MCP facade exposes the extra tools only when explicitly configured to enable this extension. A control instance without the extension does not install its models, routes or tools. Disabling it blocks new admission and first dispatch; core monitoring, cancellation and settlement of dispatched jobs continue.

## Human policy and agent authority

A project administrator configures `/projects/{project_id}/environment-build-policy/` or the CSRF-protected session API `PUT /api/v1/projects/{project_id}/environment-build-policy`. Agent keys cannot change this policy. A policy selects existing cluster-bound bootstrap environments, resource limits, source type and a compute platform/Python target:

```json
{
  "enabled": true,
  "allowed_base_environment_ids": ["11111111-1111-4111-8111-111111111111"],
  "allowed_managers": ["venv"],
  "allowed_sources": ["uploaded_wheels"],
  "resource_limits": {"cpus": 1, "memory_mib": 1024, "time_limit_seconds": 600},
  "platform_tag": "linux-x86_64",
  "python_major_minor": "3.13",
  "max_environment_bytes": 1073741824,
  "max_environment_files": 20000,
  "minimum_free_bytes": 1073741824,
  "minimum_free_files": 10000
}
```

These are illustrative values. Pin the Python version and `sysconfig.get_platform()` observed on the intended compute platform; login-node discovery is not compute validation. Builds are CPU-only, single-node jobs, with whole-minute walltime. This first builder requires a standalone system Python bootstrap. Module, Conda, Poetry and existing-venv bootstraps are rejected because preserving and validating their additional runtime initialization on reuse is not implemented. The bootstrap environment must already be permitted by ordinary project execution policy. A changed build policy, bootstrap binding or connection revision requires a fresh build and review.

Give an agent the optional `environments:build` scope together with `jobs:write`, `jobs:execute` and the read scopes needed to monitor its jobs. Uploads additionally require `objects:write`. A scope never overrides project review, compute allowance, total submission cap, active-job cap, current connection permissions or the protected worker's capabilities. Policy and scopes are checked again at admission and immediately before first dispatch. A human can reject a build on the ordinary job-review page and return a comment.

## Build API

All mutation requests use the ordinary `Idempotency-Key` header. The normal API object-upload endpoint must receive each wheel before a recipe references its hash. Current uploads are limited to 512 KiB per object; large scientific wheels and full stacks such as NumPy need a future reviewed, bounded upload/staging mechanism. This release does not fetch arbitrary URLs or increase those limits.

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

A nonempty `requirements` entry has exactly `name`, `version`, `filename`, `sha256` and `size_bytes`. The wheel filename must match its exact name and version. At most 64 distinct wheels and 100 MiB aggregate recipe wheel bytes are accepted, subject to the smaller current upload/object quotas. There are no editable sources, source distributions, environment hooks, dependency ranges or remote indexes. The caller supplies the full closure. Compute-side installation uses `--no-index --no-deps --only-binary=:all: --require-hashes`, followed by `pip check`; unresolved dependencies fail the build. Installation may still execute installed package behavior on the compute node. The built interpreter is never executed by control, the credential worker or the login-node metadata finalizer.

| Request | Result |
| --- | --- |
| `GET /api/v1/projects/{id}/environment-build-policy` | Current explicit policy and available builder |
| `GET /api/v1/projects/{id}/environment-builds` | Bounded project build list, filtered by key's cluster access |
| `POST /api/v1/projects/{id}/environment-builds` | Immutable normalized spec and a build, or a ready cache match |
| `POST /api/v1/environment-builds/{id}/prepare` with `{}` | Ordinary immutable Job package, including complete build script, recipe, lock and wheel descriptors |
| `POST /api/v1/environment-builds/{id}/execution-requests` with `{}` | Normal confirmation or budgeted admission; consumes an Attempt and ordinary lifetime submission slot when authorized |
| `GET /api/v1/environment-builds/{id}` | State, ordinary job/attempt links, receipt, estimates and published environment reference |
| `POST /api/v1/environment-builds/{id}/promote` | Human project-admin session and CSRF only; explicitly adds a ready environment revision to the project execution allowlist |

Optional MCP tools mirror policy read, create, prepare, execution request and status. They expose no policy update, environment promotion, package-manager command, shell endpoint, local-file read or SSH credential.

## Execution, publication and reuse

`EnvironmentSpec` pins project, cluster, base revision, target platform, exact lock closure, resources and storage estimate. `EnvironmentBuild` pins the policy revision/hash, creation key, ordinary Job and its single Attempt. Preparation sets `Job.extension_name=environments` before the package digest is generated. Calling the generic core job-execution API therefore invokes the same extension admission checks. A build admits only one Attempt; a failed or cancelled build needs a new build, not a reused installation directory.

The compute job creates its final prefix directly at `<protected remote_root>/<project UUID>/<job UUID>/<attempt UUID>/environment`, using copied interpreter files. It never builds elsewhere and renames the completed environment. Existing prefixes cause failure. Empty closures do not bootstrap pip. The job verifies the newly created interpreter on compute, counts files and logical bytes without following directory symlinks, and emits a bounded declared receipt with build/spec/attempt identity, target Python/platform, lock hash and timestamped measurements.

After a successful terminal observation, the extension's fixed remote operation reads metadata only. It verifies the broker's own attempt journal and exact native identity, service-derived work directory, declared recipe hash, bounded receipt, and no-follow regular-file metadata for `pyvenv.cfg` and the copied `bin/python`. Special files, symlinks and hardlinks in these checked paths fail closed. It does not import packages, run `pip`, execute the built interpreter or read SSH secrets. The control service publishes an immutable cluster/project-bound Environment only after usable final accounting has settled and matching finalization metadata passes the current policy checks.

Publishing makes the recipe visible in the project but does not authorize jobs to use it. A human administrator must explicitly promote it. Promotion updates the ordinary project environment allowlist and audit record. A fake backend marks the build `simulation_only`; it can never publish or promote an environment because it never ran the payload. Missing, failed, stale-generation, mismatched or uncertain receipts likewise cannot publish.

Cache reuse is restricted to a ready environment under the same project, connection/base revision, normalized recipe/lock/platform, and unchanged build policy revision/hash. Failed or unknown prefixes are never cache hits. A cache hit refers to the original immutable prefix; it does not claim the files still exist, attest all their contents or share them across users/projects. The cluster account remains the filesystem trust boundary. Same-UID hostile code can alter same-user data in this development deployment.

Core `WorkerReport.extension_data` durably retains the finalization metadata before the publication hook runs. A failed post-commit hook does not undo accounting. `services.sync_from_report(report)` is an internal recovery function that retries publication from that saved generation without remote execution. Missing remote finalization cannot be converted into success by an agent; there is currently no user-facing remote revalidation or prefix repair operation.

## Storage, retention and current limits

Byte/file estimates, `statvfs` headroom checks and timestamped measured bytes/files are advisory planning information. They are not an operating-system quota, a concurrent storage reservation, exact allocated filesystem blocks, or a guarantee that a shared NAS cannot fill during installation. A build can consume space before its post-install cap check fails. Operators should set conservative policy limits and reserve headroom for institutional services outside this broker.

The implemented prefix is under the protected attempt staging root. It does not yet route to an enrolled environment storage target, claim that an unrelated selected target reserved its space, or use the optional storage extension's reservations. Retain this attempt directory and the trusted bootstrap runtime for as long as jobs use its environment. Automatic cleanup, reference-counted leases, deletion, dependency repair, packed/relocatable archives, node-local staging, Apptainer images and migration across storage roots are future work. Python venv prefixes generally cannot be assumed relocatable. Conda/micromamba/Poetry builds need separate tested lock/source validation and validation receipts before being enabled.

The test suite verifies renderer purity, pinned wheel validation, ordinary approvals and budgets/caps, generic-route and dispatch fencing, explicit promotion, conservative cache reuse, fake/failed/stale/mismatched publication denial, durable metadata recovery, typed MCP tools, and a synthetic metadata finalizer whose sentinel interpreter would reveal accidental execution. These tests do not run a package installer on the control or credential worker. Actual Slurm empty-venv acceptance is a separate disposable-cluster test.
