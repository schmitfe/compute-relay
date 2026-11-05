# Optional extensions

Storage planning, environment builds, workflows and investigation records are
separate Django apps under `compute_relay.extensions`. They add policies and
metadata to ordinary core jobs, never another execution or spending path.
The core's environment recipes and per-job objects/artifacts remain available
without these apps.

## Enablement and retention

The normal launcher enables storage, environments and workflows. Developer
control/worker entrypoints treat absent or empty `BROKER_EXTENSIONS` as core only.
To change a custom deployment, stop control, select installed module names, run
migrations with the same settings/data directory and restart. Workers separately
advertise their protected extension list. Unknown or duplicate names fail;
project credentials cannot enable apps or supply import paths.

Normal MCP discovers enabled tools through authenticated capabilities; an explicit
client selection can only narrow them. Programmatic facades stay core-only unless
configured. Enablement grants no scope or project permission, and trusted module
code is not OS-sandboxed.

Disabling prevents new module admissions/dispatch, retaining tables and history.
Keep its code and data while existing jobs need observation, cancellation,
accounting or finalization. Re-enable/uninstall only with its reconciliation and
retention plan; never release uncertain reservations by removing a module.

Read module-specific setup and limits in [storage](extensions-storage.md),
[environments](environment-builds.md), [workflows](workflows.md) or
[investigations](investigations.md).

## Lifecycle hooks

The fixed registry binds prepared `extension_name`, worker capabilities and
bounded metadata to these service hooks:

| Hook | Contract |
| --- | --- |
| `validate_execution_request(job, credential)` | Validate identity before pending review |
| `validate_admission(job, credential)` | Recheck policy/scope in the admission transaction |
| `after_admission(attempt)` | Bind module state in that same transaction |
| `validate_dispatch(job, credential, attempt=...)` | Recheck before first submission |
| `worker_lease_context(attempt)` | At most 64 KiB JSON; never replace core lease fields |
| `after_report(attempt, extension_data)` | Idempotent post-commit work; cannot undo settlement |
| `before_lease(worker)` | Bounded dispatch without starving core management |
| `lease_work(worker)` | Bounded maintenance with identity, generation and fixed operation |
| `bind_job(job, credential, context)` / `after_revision(job, previous, credential)` | Atomic authorized metadata association |
| `filter_jobs(query, credential, identifier)` | Project-scoped metadata filtering |
| `validate_related_execution(job, credential)` | Related-work checks under the project lock |

Workers run only configured `execute`/`after_observation` implementations. Module
maintenance has its own authenticated report route, current generation and replay
checks; payloads are at most 64 KiB. Finalization reads metadata, never executes
workload interpreters/installers in the credentialed worker. Core
`WorkerReport.extension_data` retains metadata for safe replay after hook failures.

## Review checklist

- Keep models, migrations, routes, templates, MCP registration and tests in the
  module. Core startup must not import module models; worker imports must not
  require Django, controller models or the database.
- Check project ownership, scopes, key/cluster restrictions and referenced
  artifacts on every operation. Freeze finite inputs/context into prepared jobs;
  reject stale policy/revisions at admission and dispatch.
- Execute through core jobs so approval, compute, active/lifetime limits and
  fan-out share one ledger. Maintenance cannot become unmetered workload execution.
- Keep external I/O outside metadata locks; use durable claims, generations and
  idempotent recovery across crashes. Test independent-process SQLite races.
- Commit accounting before post-report hooks. Replay safely; failures cannot
  strand funds or undo core settlement. Do not emit raw exception contents.
- Bound and rotate scans so reviews, revoked keys and unavailable modules cannot
  starve core work. Status reads must never admit jobs.
- Test core-only, controller-only, worker-only and client-only configurations,
  unsupported dependencies, disable/re-enable, cancellation and terminal failures.
  Document prerequisites, retention, capabilities and unverified assumptions.

## Boundary check

```sh
python -m pytest tests/system/test_module_boundaries.py -q
```

These separate-process checks reject optional imports during core-only startup,
verify absent module tables/routes, and exercise authenticated synthetic jobs.
Worker imports also reject Django/controller dependencies. Test settings enabling
all modules must not alter production defaults. These checks establish dependency
boundaries; deployed HTTP/stdio, SSH, native Slurm and secret isolation require
their separate acceptance tests.
