# Investigations and completion

An investigation records a deliverable, required experiments/validation, finite
allowance and stopping rule. These guide agents; the project grant remains the
transactional spending authority. Unused funds do not justify more experiments,
and scientific closeout does not imply operational reconciliation.

## Optional investigation records

`investigations` is off by default and adds metadata, not remote execution.
Enable it alongside existing modules, for example
`--extensions storage,environments,workflows,investigations`, then reconnect MCP.
Direct deployments add it to `BROKER_EXTENSIONS`, migrate with existing settings
and restart. [Extension retention](extensions.md#enablement-and-retention) applies;
its closed-scope check is unavailable while disabled.

Read needs `jobs:read`; creation/grouping/closeout needs `investigations:write`;
optional cancellation also needs `jobs:cancel`. Existing keys gain no new scope.

1. `create_investigation(name, plan)` records the plan.
2. `prepare_job(..., investigation_id=..., role="core")` groups new packages.
   Roles are `core`, `validation`, `exploration`; revisions inherit membership.
   `add_investigation_jobs` attaches existing jobs, at most 32 per call.
3. `get_investigation` summarizes blockers, abandoned packages, roles, charges and
   reservations. List/filter with `list_investigations` or
   `list_project_jobs(..., investigation_id=...)`.
4. `finish_investigation` records `sufficient`, `inconclusive` or `user_stopped`
   and a report, blocking new execution while enabled. `cancel_unstarted` dismisses
   never-requested packages/pending reviews; admitted attempts need normal
   cancellation/reconciliation. Closing never invents final accounting.

Each group holds at most 128 packages. Creation snapshots the key's accessible
cluster scope, requiring at least one cluster; later project changes/sibling keys
cannot widen it. Reads and changes need current access to every saved cluster,
even for empty groups. Jobs/revisions and producer artifacts must fit that scope;
explicit uploads create independent copies. Membership is stable, grouped creation
atomic, and equivalent-authority keys can collaborate/rotate. Current input
provenance and revocations still apply.

Plans require `question`, `deliverable`, `core_experiments`, `required_validation`,
`stopping_rule`, `budget` and `diagnostic_limit`. Budgets use nonnegative integer
CPU/GPU seconds and `max_executions` 1–10,000; diagnostics count 0–32 exploration
attempts/pending reviews. Count pilots, probes and retries in the local plan even
when tools do not attach them. Idempotent transport replay adds no execution.
Summaries flag limits using usage/reservations/attempts/reviews; they are not
hard investigation sub-budgets or automatic experiment schedulers.

## Completion and local evidence

Core `get_job_completion` needs no module. It returns provenance, hashes, execution
requests/attempts, artifact descriptors, collection/accounting and per-key access
state, never artifact contents.

HTTP completion pages attempts with `cursor` and requests with `execution_cursor`,
independently, at `limit` 1–20. Follow both next cursors; summary/revision covers
all pages. Blockers expose at most 20 details with `blocker_count` and
`blockers_truncated`. Ordinary status defaults to latest 20 attempts/requests,
with previous/next cursors for older pages. Reads use consistent SQLite snapshots
without a writer lock; revision checks scan compact metadata/report hashes, not
off-page logs/artifacts. Human review keeps its full history.

`get_job.execution_progress` distinguishes:

| Field | Meaning |
| --- | --- |
| `client_permission_review: not_observed_by_broker` | Client approval/timeout must be learned from the client |
| `execution_request_exists` | A request exists, not necessarily a submission |
| `dispatch_started` | Durable submission intent |
| `compute_submitted: true / null / false` | Native receipt / uncertain submission / neither recorded |

`operationally_reconciled` covers execution, collection and accounting. Verified
local delivery, acknowledgement and scientific validity remain independent.
A byte request proves neither retention nor verification; acknowledgement is not
scientific certification. Resource-seconds are not monetary cost.

Use the installed interpreter in `.compute-relay/USAGE.md`:

```sh
python -m compute_relay.cli files finish --directory /path/to/workspace \
  --investigation-id INVESTIGATION_UUID --output results/INVESTIGATION --acknowledge
```

`--job-id JOB_UUID` exports one job without investigations. `--no-download` marks
delivery unverified; `--resume` requires matching broker/project/subject and
rechecks hashes/states. Private outputs and atomic `run-manifest.json` record
provenance, receipts, pending exports, usage and separate completion states, at
most 32 MiB metadata. This never starts compute, retries execution, changes grants,
closes scope or certifies science.

Per-job pagination rechecks revision, retries an unstable snapshot once, then
fails. Group exports hold per-job revisions, not a global transaction; changes
at final read populate `pending_job_exports` and prevent a complete claim.

## Bounded waiting and shared guidance

`get_job`/`get_investigation` return `revision` and `changed`. Pass the opaque
revision as `after_revision` and integer `wait_seconds` 0–20; positive waits need
a revision. Sleeping holds no DB transaction and rechecks authority. There are two
wait slots; excess returns `WAIT_BUSY` (429), unchanged timeout `changed: false`.
MCP resources and `get_compute_guide` expose workflow, placement, completion and
job-request guidance on demand.

## Verification

```sh
docker compose -p relay-investigation-test -f deploy/compose.test.yml \
  -f deploy/compose.investigations-test.yml up -d --build --wait control slurm worker
docker compose -p relay-investigation-test -f deploy/compose.test.yml \
  -f deploy/compose.investigations-test.yml exec -T control python /opt/investigation_smoke.py \
  --demo-file /run/demo/demo.json --confirm-disposable-fixture
docker compose -p relay-investigation-test -f deploy/compose.test.yml \
  -f deploy/compose.investigations-test.yml down --volumes
```

This disposable native fixture reviews one CPU job, cancels unused work, checks
export/resume/hashes and the shared ledger. It is not institutional acceptance.
Planning guidance adapts [Diagnosing Bugs](https://github.com/mattpocock/skills/blob/main/skills/engineering/diagnosing-bugs/SKILL.md)
and [Prototype](https://github.com/mattpocock/skills/blob/main/skills/engineering/prototype/SKILL.md),
with shared-plan principles from [Anthropic](https://www.anthropic.com/research/multi-agent-research-system)
and planned/exploratory analysis from [COS](https://www.cos.io/initiatives/prereg).
