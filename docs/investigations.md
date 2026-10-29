# Bounded investigations and completion

The compute skill starts with a concrete deliverable, a minimum experiment set,
required validation, a finite resource/execution allowance, a diagnostic limit
and an evidence-based stopping rule. All agents and retries share that plan.
Unused project funds do not justify another experiment. Failed required
validation with no remaining allowance yields an inconclusive report. The
[planning comparison](references/investigation-planning.md) records the sources
and limitations behind these rules.

## Optional investigation records

The `investigations` module stores plans and groups ordinary jobs. It is disabled
by default, including on existing installations. Enable it explicitly alongside
the modules already selected, for example with the local launcher's
`--extensions storage,environments,workflows,investigations`. The launcher applies
the module's new migration on startup using the same private data directory.
For direct process deployments, stop control, add `investigations` to
`BROKER_EXTENSIONS`, run `python -m django migrate` with the existing deployment
settings/data directory, then restart. Retain the old selections. Reconnect the
MCP client so it discovers the additional tools.

Read operations require `jobs:read`. Creation, grouping and closeout require the
new explicit `investigations:write` scope; cancelling unused packages additionally
requires `jobs:cancel`. Existing project keys gain no scope automatically. The
module performs no remote operations and needs no new worker execution capability:
its members remain ordinary jobs or jobs owned by another enabled extension.
Disabling this metadata module retains its tables and ordinary core accounting;
its grouping reads and closed-scope admission check are unavailable until it is
re-enabled. It must remain enabled to enforce closed investigation scope.

Use `create_investigation(name, plan)` to record the plan, then
`prepare_job(..., investigation_id=..., role="core")`. Roles are `core`,
`validation`, and `exploration`. Existing jobs can be attached with
`add_investigation_jobs`; revisions inherit the same group and role. A job has
one stable membership. A group holds at most 128 packages, and attachment accepts
at most 32 explicit job IDs per call. Grouped creation is atomic: a denied group
cannot leave an orphan job. Each member's connection and input provenance remain
subject to the current key's authorization.

`plan` requires `question`, `deliverable`, `core_experiments`,
`required_validation`, `stopping_rule`, `budget`, and `diagnostic_limit`.
The typed MCP schema exposes lengths and bounds. `budget` contains nonnegative
integer `cpu_core_seconds` and `gpu_device_seconds`, plus `max_executions` from
1 to 10000. A diagnostic limit of 0 through 32 counts exploration attempts and
pending reviews. Count environment probes and new execution attempts in the same
local plan even when their creating tool does not attach them automatically.
An idempotent transport retry does not add a new execution.

These plan limits are **agent guidance**, not a second admission ledger or hard
investigation cap. The summary flags limits reached using settled charges,
outstanding reservations, attempts and pending approvals. The existing project
grant still enforces compute admission transactionally. A structured plan does
not schedule its listed experiments or establish scientific validity.

`list_investigations` filters open, closed or all records.
`list_project_jobs(..., investigation_id=...)` retains the ordinary attention/
state filters and pagination. `get_investigation` returns member roles, abandoned
packages, per-job blockers, aggregate settled usage and outstanding reservations.
`finish_investigation` records `sufficient`, `inconclusive` or `user_stopped` and
a closing report. It blocks new execution requests while enabled. Its explicit
`cancel_unstarted` option cancels never-requested drafts/prepared packages and
pending reviews. Already admitted attempts need normal attempt cancellation and
reconciliation; uncertain reservations remain visible. Closing the report does
not wait for speculative extensions or pretend that accounting is final.

## Completion and local evidence

Core `get_job_completion` works without the module. It returns package hashes,
input descriptors, environment/resource identity, execution requests, paginated
attempts, retained artifact descriptors, collection state, ledger charges,
reservations and per-key acknowledgement. It sends no artifact contents.
`get_job` includes a compact completion summary and `execution_progress`:

| Observation | Meaning |
| --- | --- |
| `client_permission_review: not_observed_by_broker` | Client approval success or timeout must be learned from the client |
| `execution_request_exists` | A broker request is recorded; this alone does not mean submitted |
| `dispatch_started` | Submission intent is durable; receipt may still be unknown |
| `compute_submitted: true` | A native scheduler receipt is recorded |
| `compute_submitted: null` | Dispatch is uncertain; reconcile the original attempt |
| `compute_submitted: false` | No confirmed or currently uncertain submission is recorded |

`operationally_reconciled` covers execution, collection and accounting blockers.
Local download verification, acknowledgement and scientific acceptance stay
separate. A request for bytes cannot prove that the client retained and verified
them. Acknowledgement does not certify a scientific claim. Resource-seconds are
accounting figures, not monetary costs.

Read the workspace's `.compute-relay/USAGE.md` for the exact installed interpreter.
The new helper operation consolidates metadata export and result delivery:

```sh
python -m compute_relay.cli files finish --directory /path/to/workspace \
  --investigation-id INVESTIGATION_UUID --output results/INVESTIGATION --acknowledge
```

Use `--job-id JOB_UUID` for an individual job; no investigation module is needed.
`--no-download` exports metadata with local delivery explicitly unverified.
`--resume` reuses only a matching broker/project/subject export, rechecks hashes,
and refreshes states. Outputs are private regular files under the explicit
workspace destination. `run-manifest.json` includes provenance, job revisions,
local receipts, pending exports, costs in resource-seconds, reservations and
separate operational/local/scientific states. It is capped at 32 MiB of metadata.
Publication is atomic. The helper never starts compute, retries execution,
changes grants, closes investigation scope or certifies the science.

Each job's pages share a revision checked again after pagination; an unstable
snapshot is retried once, then fails explicitly. Group exports record per-job
revisions, not a global transactional snapshot. Jobs added or changed before
the final group read appear in `pending_job_exports` and prevent a complete
claim. The manifest is evidence as observed, not a lock on future broker changes.

## Bounded waiting and shared guidance

`get_job` and `get_investigation` return an opaque SHA-256 `revision` and
`changed`. Supply `after_revision` and integer `wait_seconds` from 0 through 20
for a bounded wait. A positive wait requires a revision. Each poll rechecks
authority. Sleeping holds no database transaction, and two wait slots leave
the remaining HTTP threads available; excess waits return `WAIT_BUSY` (429).
An unchanged timeout returns `changed: false`. Persist the returned revision.

MCP advertises `compute-relay://guide/workflow`, `/placement`, `/completion`
and `/job_request`. `get_compute_guide(topic=...)` provides the same content
for tool-only clients. Tool descriptions focus on their own operation. Resource
fields expose primitive types, constraints, units and CPU/GPU examples without
the conditional intersection that some clients render as `unknown & unknown`.
Canonical validation still enforces GPU type/count consistency before mutation.
Generic GPU requests are allowed only on the connection's advertised
`untyped_gpu_partitions`; the broker rechecks project permission at admission
and dispatch.
Host `memory_mib` requests RAM; GPU VRAM depends on the selected hardware.

## Verification

The unit/HTTP suites cover grouping authority and atomicity, inherited revisions,
uncertain dispatch, closeout, waits, missing outputs, paginated snapshot changes,
hash verification, safe resume, and acknowledgement without execution. Core-only
boundary tests prove the module can be absent. Run native fixture acceptance with:

```sh
docker compose -p relay-investigation-test -f deploy/compose.test.yml \
  -f deploy/compose.investigations-test.yml up -d --build --wait control slurm worker
docker compose -p relay-investigation-test -f deploy/compose.test.yml \
  -f deploy/compose.investigations-test.yml exec -T control python /opt/investigation_smoke.py \
  --demo-file /run/demo/demo.json --confirm-disposable-fixture
docker compose -p relay-investigation-test -f deploy/compose.test.yml \
  -f deploy/compose.investigations-test.yml down --volumes
```

This uses one explicitly reviewed CPU job on the disposable SSH/Slurm fixture,
cancels an unused package and pending review, runs the installed export CLI and
resume, verifies result bytes/hashes and checks the shared ledger. It is not a
test against an institutional cluster or evidence of scientific validity.
