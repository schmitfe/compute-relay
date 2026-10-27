# Recovering agent work and choosing available compute

`get_project_context` includes `work_summary` for keys with `jobs:read`. After a
restart or lost conversation context, call `list_project_jobs()` and follow
`next_cursor` to recover durable project work before preparing more executions.
The API key selects the project; current key cluster restrictions still apply.

The default `attention` filter includes drafts/prepared jobs that have never been
submitted, pending human reviews, active execution, unresolved accounting/output
collection and finished results not acknowledged by this key. Superseded
unsubmitted revisions are omitted. The explicit filters are `unfinished`,
`unread_results` and `all`; pages contain at most 100 jobs and bounded attempt
summaries. `get_job` supplies details for an individual recovered job.

Result tracking is durable per credential and attempt. Listing artifacts records
inventory access; requesting terminal logs or artifact content records a byte
request. This is not proof of a complete verified download. After reviewing or
recording results, `acknowledge_job_results(attempt_id)` clears the reminder for
that key without asserting scientific validity or changing accounting. Historic
reads before this migration are unknown and may appear unread. A newly issued
key sees project work, but has its own result acknowledgements.

Listing and reconnecting never execute or resubmit a job. Reuse existing
execution-request/attempt identities and idempotency keys when recovering an
uncertain operation. `cancel_prepared_job(job_id)` dismisses an abandoned draft
that has no execution requests and retains its package/history. Submitted work
uses the existing execution-request/attempt cancellation tools.

`request_execution(job_id)` derives its default idempotency key from the immutable
job ID. Repeating it, including after an MCP restart, returns the original
execution request without another reservation or submission. Use a new explicit
`idempotency_key` only when you intend to execute that same package again.

| API | Purpose |
| --- | --- |
| `GET /api/v1/projects/{id}/jobs?state=attention&limit=20&cursor=...` | Key-scoped recovery inbox |
| `POST /api/v1/attempts/{id}/acknowledge-results` with `{}` | Acknowledge a terminal result for this key |
| `POST /api/v1/jobs/{id}/cancel-prepared` with `{}` | Cancel a never-submitted draft; requires `jobs:cancel` |
| `GET /api/v1/projects/{id}/availability` | Cached scheduler observations across permitted clusters |
| `POST /api/v1/projects/{id}/availability` with `{}` or `connection_ids` | Request bounded observation refreshes |

## Admission limits and scheduler availability

`effective_limits` and the allowance describe what a project can request.
`get_compute_availability(refresh=True)` requests fresh read-only observations
through the existing authenticated connector. Poll with `refresh=False`; each
cluster is rate limited to one refresh per 30 seconds. The updated worker
advertises `availability_v1`. Old connectors return `connector_upgrade_required`
instead of pretending a snapshot is available.

Snapshots show scheduler-visible partition node/CPU counts, configured and
allocated/idle GPUs by type, running/pending queue entries, GPU request types
and pending reasons. Every observation has a timestamp, age, query status,
truncation indicator and connection revision. Data older than 120 seconds, or
from an old connection revision, is stale. Unknown allocations stay unknown;
failed scheduler queries do not contribute capacity. Queries use fixed `sinfo`
and `squeue` formats with five-second deadlines and bounded returned data; they
do not allocate compute or modify cluster configuration.

`queue_pressure.pending_jobs_per_schedulable_node` normalizes pending entries by
the partition's schedulable node count. For example, 10 pending jobs on 5 nodes
is 2 per node; 100 on 200 is 0.5. The larger absolute queue can have less relative
pressure. Ratios use decimal strings (for example, `"2"`, `"0.5"`) with
`ratio_encoding: "decimal_string"`, or `null` when unknown. Compare them
numerically, not lexicographically; counts and capacities remain integers.
`pending_gpu_jobs_per_schedulable_device` uses the capacity of each GPU
type on schedulable nodes, excluding down/draining nodes. The raw counts and
denominators remain visible. Zero/missing capacity or failed/truncated queries
produce unknown ratios. Compare relevant partitions across clusters; their
totals may overlap. Typed GPU ratios exclude untyped GPU requests, which are
reported separately. Ratios count visible queue entries, not requested resource
amounts or durations; they cannot predict start times or rank clusters alone.

Partition totals can overlap. Idle totals may be fragmented across nodes or
unavailable under account, QOS, reservation or priority rules. Queue visibility
can be restricted by the site, and array jobs may be grouped. This provides
placement evidence, not a reservation, exact wait estimate or guaranteed start.
The workflow selects among permitted clusters; automatic placement and monetary
cost comparison are not configured. A future pricing extension can add those
without treating current job ceilings as available hardware.

These observations use Slurm's documented [node fields](https://slurm.schedmd.com/sinfo.html)
and [queue fields](https://slurm.schedmd.com/squeue.html).

## Updating an installation

Apply both core and enabled environment-extension migrations, restart control
and connectors, and restart project MCP clients to load the new tools and server
guidance. Review/update an existing locally installed compute workflow skill
against the bundled version; initialization preserves differing local skills.
Project administrators can enable automatic
permission of verified environment builds once in environment-build policy.
The code changes alone do not change the running service, deployed connectors,
existing build policy or real cluster state.
