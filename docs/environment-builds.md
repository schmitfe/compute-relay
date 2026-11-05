# Cluster environments

Owners register existing cluster-bound recipes; agents may use only recipes
explicitly granted to their project. The bundled `environments` extension also
builds Python venvs as charged Slurm jobs. Conda, Poetry, micromamba and existing
venv installation bootstraps are unsupported; existing recipes remain usable.
Module enablement and retention follow the [extension contract](extensions.md).

## Cluster recipes and inspection

Open **Clusters → your cluster → Environments → Add environment**:

1. Choose existing Python, Modules/Lmod, Conda, Poetry venv or Python venv.
2. Select exact modules in load order or enter names manually. Conda/venv/Poetry
   recipes may retain module loads. Initialization is optional; the job normally
   uses the cluster's module command. A missing command fails explicitly.
3. With the connector running, select **Inspect selection**, then choose detected
   Python/environment paths or enter existing absolute paths manually. Changed
   modules require fresh inspection. Save the immutable recipe and grant it to
   the project. The same host under another SSH account has separate recipes.

Inspection is a human, CSRF/revision-checked login-node operation. It loads only
selected modules and checks executable/file metadata without running discovered
Python interpreters. Fixed `conda info --envs --json` runs with plugins/config
disabled and isolated HOME; bounded `.conda/environments.txt` metadata adds
prefixes. Poetry discovery uses `~/.cache/pypoetry/virtualenvs`; other locations
need manual paths. No login-node installation or Poetry project hook runs.
Results are timestamped observations, not compute-node validation. A scheduled
cluster check verifies the selected recipe on compute. Job behavior is:

| Recipe | Compute execution |
| --- | --- |
| Python/Bash | Selected existing interpreter |
| Modules/Lmod | Login batch shell, ordered loads, selected interpreter |
| Python venv | Prefix/PATH and interpreter, without activation hooks |
| Conda | Selected executable with `conda run --prefix` |
| Poetry venv | Existing interpreter, without Poetry project hooks |

Module jobs restore the submission directory after site startup. An optional
explicit initializer is fallback only when the module command is absent;
`MODULE_COMMAND_UNAVAILABLE` fails instead of using a different environment.

## Human policy and agent authority

The owner enables builds at `/projects/{id}/environment-build-policy/` or the
CSRF-protected human `PUT /api/v1/projects/{id}/environment-build-policy` route.
Keys cannot change this policy. An illustrative policy is:

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

Pin the Python/platform observed on compute, not just the login node. Builds are
CPU-only, single-node, whole-minute jobs using an already permitted system or
module Python. Changed policy, bootstrap binding or connection revision requires
a new build/review. Give the key `environments:build` and ordinary job/read scopes;
wheel uploads need `objects:write`. Normal admission and dispatch checks apply.
`auto_permit_builds` defaults false; enabling it authorizes verified revisions for
ordinary project jobs, without bypassing their compute policy.

## Build API

Mutation requests use `Idempotency-Key`. Stage exact wheels through the
[resumable file helper](data-transfer.md#upload-inputs-and-download-results).
No package-name resolver or arbitrary URL/index fetch is implemented.

Create an empty closure with `POST /api/v1/projects/{id}/environment-builds`:

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

Each nonempty requirement has exactly `name`, `version`, `filename`, `sha256` and
`size_bytes`; the filename must match. Supply the complete closure, at most 64
wheels within transfer/quotas. No source distributions, editable inputs, ranges or
installer hooks are accepted. Compute installation uses
`--no-index --no-deps --only-binary=:all: --require-hashes`, then `pip check`.
Installed-package behavior can execute on compute, never in control or the
credentialed connector. Empty closures do not bootstrap pip.

| Route | Purpose |
| --- | --- |
| `GET /api/v1/projects/{id}/environment-build-policy` | Current policy/bootstrap choices |
| `GET /api/v1/environments/{id}/inventory` | Runtime inventory and drift status |
| `POST /api/v1/environments/{id}/inspect` | Prepare/request or resume a scheduled probe |
| `POST /api/v1/environments/{id}/packages` | Build/update from pinned uploaded wheels |
| `GET/POST /api/v1/projects/{id}/environment-builds` | Scoped list / immutable spec and build or ready cache match |
| `POST /api/v1/environment-builds/{id}/prepare` | Ordinary reviewable package |
| `POST /api/v1/environment-builds/{id}/execution-requests` | Normal reviewed/budgeted attempt |
| `GET /api/v1/environment-builds/{id}` | State, job, receipt and published reference |
| `POST /api/v1/environment-builds/{id}/promote` | Human/CSRF-only project allowlist addition |

MCP exposes corresponding read/build tools, not policy edits, promotion or shell
commands. `request_environment_packages` combines creation, preparation and the
execution request. Updates keep the approved bootstrap and pinned packages except
names explicitly replaced, creating a fresh prefix/revision. A ready cache hit
submits nothing.

### Agents requesting a new environment

Read `get_environment_build_policy` and `get_environment_inventory` first.
`inspect_environment` schedules a bounded probe or resumes its existing request.
Omitted resources retain the pending request; sole scheduler choices are filled,
while ambiguous partition/account/QoS values must come from current targets.
Fix a draft probe through `inspect_environment` with corrected arguments and a
new idempotency key; generated inputs cannot use ordinary `revise_job` uploads.
Once execution is requested, poll it or cancel before changing settings.

Use `request_environment_packages`, or the granular `create_environment_build`,
`prepare_environment_build`, `request_environment_build` and
`get_environment_build` tools. Installation/verification runs in the scheduled
job. Publication and permission are separate states; wait for a ready, permitted
revision before using it.

## Execution, publication and reuse

A spec pins project, connection/base revision, platform, exact closure, resources
and storage estimate. Its build pins policy/hash, creating key, ordinary job and
one attempt. Core execution routes enforce the same extension checks. A failed
or cancelled build needs a fresh build; it cannot reuse an installation directory.

The final prefix is `<remote_root>/<project>/<job>/<attempt>/environment`.
It is created there directly with copied interpreter files, never renamed into
place; an existing prefix fails. The compute receipt binds identities, lock hash,
Python/platform, inventory and measured bytes/files. Module-based builds retain
module names, paths and optional initializer for later jobs.

Finalization verifies the journal/native identity, derived directory, recipe and
bounded receipt, plus no-follow ordinary `pyvenv.cfg`/`bin/python` metadata.
Links/special files in those checked paths fail. It runs no interpreter or pip.
Control publishes only after successful execution, definitive settled accounting
and matching current-policy finalization. Fake, failed, uncertain, mismatched or
unaccepted results cannot publish. An accepted collection receipt keeps its original
generation when later accounting polls advance the attempt. Automatic permission needs
a complete verified inventory and still-authorized creating key; otherwise a
human promotes the revision.

Inventory fingerprints cover observed Python/platform/package names and versions,
not all package file contents. Later scheduled inspection reports drift. Ready
cache reuse requires the same project, connection/base, normalized recipe/platform
and policy revision/hash; it neither reattests the prefix nor shares it across
projects. Same-UID cluster code can modify these files.

`WorkerReport.extension_data` preserves finalization before post-commit hooks.
Normal worker polling retries up to four settled unpublished builds from those
accepted reports, with a durable 30-second backoff. Publication uses the same
validation and locking as the initial hook; replay cannot rebuild, duplicate an
environment or charge again. Missing/invalid finalization cannot be declared
successful; no user-facing prefix repair/revalidation operation exists.

## Storage, retention and current limits

Byte/file estimates and post-install caps are advisory, not disk reservations or
OS quotas. A build can consume space before failing a cap; leave headroom for
other users/services. Prefixes use attempt staging, not selected storage targets.
Retain the prefix and trusted bootstrap while jobs use them. No automatic cleanup,
repair or cross-root migration is provided; venvs are not assumed relocatable.

## Opt-in disposable Slurm acceptance

Use a fresh synthetic fixture project:

```sh
BROKER_TEST_PORT=18767 docker compose -p cb-environment-acceptance -f deploy/compose.test.yml -f deploy/compose.environments-test.yml up --build -d control worker
BROKER_TEST_PORT=18767 docker compose -p cb-environment-acceptance -f deploy/compose.test.yml -f deploy/compose.environments-test.yml run --rm smoke
BROKER_TEST_PORT=18767 docker compose -p cb-environment-acceptance -f deploy/compose.test.yml -f deploy/compose.environments-test.yml down -v
```

`tools/environment_build_smoke.py` requires fixture confirmation/metadata, refuses
fake/nonfixture targets and reads no SSH/worker credential. It builds an approved
venv from `fixture-python/1.0` with a pinned synthetic wheel, verifies automatic
publication under owner permission, and imports the wheel in a follow-up job.
It repeats with wheel version 2.0 in a new immutable prefix. Checks include output
hashes, module retention, cache reuse, four admissions and zero reserved compute.
Default target is Bookworm `linux-x86_64`/Python `3.11`; other fixture
architectures need explicit target options. Cleanup applies only to that named
stack. Unit/configuration checks do not establish that this native run passed.

Discovery interfaces: [Conda info](https://docs.conda.io/projects/conda/en/stable/commands/info.html),
[Poetry environment locations](https://python-poetry.org/docs/configuration/).
