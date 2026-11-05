# Recover work and inspect scheduler availability

After restart/context loss, read `get_project_context.work_summary`, then
`list_project_jobs()` and follow `next_cursor` before creating more work.
The key selects its project and current cluster restrictions.

Default `attention` includes unused drafts, pending reviews, active execution,
unresolved accounting/collection and this key's unacknowledged results, excluding
superseded unused revisions. Other filters are `unfinished`, `unread_results`,
`all`; pages contain at most 100 bounded summaries. Inspect individual `get_job`.

Result access/acknowledgement is per key and attempt. Inventory/byte requests do
not prove verified delivery. `acknowledge_job_results(attempt_id)` clears that
key's reminder after assessment, without claiming science or changing accounting.
A new key has its own acknowledgements.

Listing/reconnecting never executes work. Reuse request/attempt identities and
stable idempotency keys after uncertain replies. Default `request_execution(job_id)`
reuses that job's first request; only a fresh explicit key deliberately repeats
execution. `cancel_prepared_job` dismisses a never-requested draft while retaining
evidence; admitted work needs normal cancellation/reconciliation.

| Route | Purpose |
| --- | --- |
| `GET /api/v1/projects/{id}/jobs?state=attention&limit=20&cursor=...` | Scoped recovery inbox |
| `POST /api/v1/attempts/{id}/acknowledge-results` with `{}` | This key's result acknowledgement |
| `POST /api/v1/jobs/{id}/cancel-prepared` with `{}` | Dismiss unused draft; `jobs:cancel` required |
| `GET/POST /api/v1/projects/{id}/availability` | Read cached / request refreshed permitted cluster observations |

## Admission limits and scheduler availability

`effective_limits`/allowance describe authorization, not idle hardware.
`get_compute_availability(refresh=True)` requests read-only observations; poll
with `refresh=False`. Refresh is limited to once per cluster per 30 seconds.
Connectors advertise `availability_v1`; incompatible ones return
`connector_upgrade_required`.

Snapshots include partition CPU/node/GPU counts, allocated/idle hardware,
visible queue entries/reasons, timestamp/age/status/truncation and connection
revision. Age over 120 seconds or an old revision is stale; failed queries and
unknown allocation remain unknown. Fixed `sinfo`/`squeue` queries have five-second
deadlines and bounded data; they neither allocate nor change configuration.

`queue_pressure.pending_jobs_per_schedulable_node` divides pending entries by
schedulable nodes; `pending_gpu_jobs_per_schedulable_device` does so per GPU type,
excluding down/draining nodes and reporting untyped jobs separately. Values are
decimal strings (`ratio_encoding: decimal_string`) or null: compare numerically,
not lexicographically. Raw counts/denominators remain visible. Zero/missing
capacity or failed/truncated observations gives unknown ratios.

Overlapping partitions, fragmentation, reservations, account/QoS/priority rules,
restricted queue visibility and grouped arrays limit these observations. Ratios
count entries, not job sizes/durations. They do not predict start times, reserve
capacity, rank clusters alone or estimate money. Automatic placement/pricing is
not configured. Source semantics: [Slurm nodes](https://slurm.schedmd.com/sinfo.html)
and [queues](https://slurm.schedmd.com/squeue.html).
