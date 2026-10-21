# Optional extensions

The core broker submits ordinary reviewed/budgeted jobs and keeps their accounting.
Optional functionality lives in separate Python/Django apps under
`compute_broker.extensions`. The default `BROKER_EXTENSIONS` is empty: no optional
app, migration, URL or new execution capability is enabled implicitly.

An operator can enable installed modules with, for example,
`BROKER_EXTENSIONS=storage,environments,workflows`. Stop the control process, enable
the selected apps, run Django migrations with the same settings/data directory,
then restart. A feature's presence in source is not evidence of runtime readiness;
consult its own documentation and acceptance tests. Unknown/duplicate module names
fail configuration rather than importing arbitrary code.

Each module owns its models, migration history, validators, services, URLconf, forms,
templates and tests. These are modules within one repository, allowing atomic
reviews; they can later move into separately installed distributions behind the
same lifecycle contract. Installation and enablement belong to the trusted operator.
Neither a project key nor project settings can install or enable an extension.

The core integration surface consists of a prepared job's `extension_name`, worker
capabilities, bounded lease/report metadata, and these optional service hooks:

| Hook | Contract |
| --- | --- |
| `validate_execution_request(job, credential)` | Check extension-specific request identity before creating a pending review |
| `validate_admission(job, credential)` | Recheck extension-specific policy/scope inside the core admission transaction |
| `after_admission(attempt)` | Bind extension state to the durable ordinary attempt in the same transaction |
| `validate_dispatch(job, credential, attempt=...)` | Recheck current extension policy before a new scheduler submission |
| `worker_lease_context(attempt)` | Return at most 64 KiB of JSON metadata; cannot replace core lease fields |
| `after_report(attempt, extension_data)` | Idempotent post-commit processing; failures cannot undo compute settlement |
| `before_lease(worker)` | Perform bounded durable extension dispatch without preventing core observation |
| `lease_work(worker)` | Return no work or `{id, generation, configuration, context}` for bounded maintenance; core binds extension and operation names |

Workers independently opt in using their private `extensions` list or the
`BROKER_EXTENSIONS` process setting. They advertise supported modules to control.
New extension jobs require both control enablement and the assigned worker's
capability, in addition to normal scopes/project limits. On terminal observations,
an enabled module's `worker.after_observation` can run its fixed metadata adapter;
it must not execute payloads or install packages in the credentialed process.
Report metadata is durably retained in `WorkerReport.extension_data` for module
recovery. Hook failures are recorded without emitting raw exception contents.

For maintenance leases, `worker.execute(worker_instance, lease)` runs only when
the extension is explicitly configured on that worker. Each extension owns its
worker-authenticated report route and lease replay/expiry checks. Maintenance
payloads are limited to 64 KiB, cannot replace ordinary attempt fields, and must
be scheduled with bounded frequency so normal accounting remains responsive.

Bundled additions are [storage planning](extensions-storage.md),
[environment builds](environment-builds.md), and [artifact workflows](workflows.md).
They are independent apps. Neither storage planning nor workflows implicitly
enables environment creation, remote migration, or software downloads.

Disabling a module prevents new admissions and undispatched extension jobs. Already
submitted jobs continue through the core observation, cancellation and accounting
paths. Keep its tables, runtime implementation and version available until its
dependent jobs/builds/transfers have completed; removing source is a separate
operator migration. Re-enabling a module may require its documented reconciliation
procedure before publishing extension results.

`GET /api/v1/capabilities` reports enabled modules to an authenticated project key.
The endpoint does not grant scopes or project policy. Test settings enable bundled
apps that actually exist in the checkout; ordinary settings retain opt-in behavior.
This does not create an OS sandbox for trusted plugin code: review and install it
with the same care as the worker itself.
