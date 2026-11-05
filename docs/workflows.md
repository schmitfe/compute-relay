# Artifact workflows

The bundled workflows app turns a finite dependency graph into ordinary jobs.
Enablement/retention follows [extensions](extensions.md); every node still needs
normal job authority, review, resources and allowance.

A revision contains at most 32 named nodes. Each has a job request, optional
`depends_on` names and `inputs` edges with producer `node`, `output` and destination
`path`. Graphs must be acyclic; artifact inputs must be declared producer outputs
and cannot overwrite code/other inputs. Only successful, settled producers can
unlock consumers. A missing output blocks them even after successful compute.

Branch/join graphs are supported: `start → [left, right] → join`, with both branches
depending on start and join depending on both. These are broker dependencies,
not Slurm arrays/dependency flags. For intermediate analysis use one job per chunk;
growing files in running jobs are not artifacts. Live stdout/stderr is separate.

Edges bind artifact ID, hash and exact producing attempt, relayed on the same
cluster. Cross-cluster edges and arbitrary remote-path transfers are unsupported.
Each ready node receives its own immutable package and execution request. Human
approval applies per node; rejection blocks descendants. Change rejected content
through a new workflow revision; there are no automatic retries or DAG-wide approvals.

## API and portal

| Action | Endpoint |
| --- | --- |
| Seal graph | `POST /api/v1/projects/{id}/workflow-revisions` |
| Start run | `POST /api/v1/workflow-revisions/{id}/runs` |
| Read | `GET /api/v1/workflow-runs/{id}` |
| Advance | `POST /api/v1/workflow-runs/{id}/advance` |
| Cancel | `POST /api/v1/workflow-runs/{id}/cancel` |
| Human list/review | `/projects/{id}/workflows/`, `/workflows/{id}/` |

MCP tools are `prepare_workflow`, `start_workflow`, `get_workflow`,
`advance_workflow`, `cancel_workflow`. `workflows:write` supplements `jobs:write`
for revisions, `jobs:execute` for start/advance, `artifacts:read` for edges and
`jobs:cancel` for cancellation. Runs retain the creating key; revocation stops new
nodes, never transfers ownership to another key. Permissions are rechecked when
nodes become ready. Existing submitted jobs keep core observation/accounting.

Create/start/cancel use idempotency keys; advance durably reuses node/job/request
identities. Status GETs never admit work. Workers rotate at most four due runs,
advancing two eligible nodes each; pending reviews cannot starve unrelated jobs.
A two-minute preparation claim recovers crashes. File preparation stays outside
metadata transactions; admission uses the core serialized ledger. Capacity waits
resume the same node rather than rejecting policy or multiplying attempts.

## Disposable native workflow acceptance

`tools/workflow_smoke.py` requires `--confirm-disposable-fixture` and synthetic
metadata for `slurm-fixture`, `brokerjob` and its staging root. It refuses fake/
nonfixture targets, reads no SSH/worker credentials and uses the synthetic owner
session plus a limited key. Its review-only project has exactly four lifetime
slots for:

1. `produce`: `[1,2,3]` plus newline.
2. Independent `double` and `triple` branches: `[2,4,6]` and `[3,6,9]` from that artifact.
3. `sum`: `{"sum":30}` only after both branches finish and provide their artifacts.

Checks require reviewed dependency identities/hashes, one request/attempt per
node, all four slots consumed, positive native CPU usage and zero reservation.
Repeated advance preserves identities; revoking the creating key denies further
advance with HTTP 401. Use a fresh named disposable stack:

```sh
BROKER_TEST_PORT=18768 docker compose -p cb-workflow-acceptance -f deploy/compose.test.yml -f deploy/compose.workflows-test.yml up --build -d control worker
BROKER_TEST_PORT=18768 docker compose -p cb-workflow-acceptance -f deploy/compose.test.yml -f deploy/compose.workflows-test.yml run --rm smoke
BROKER_TEST_PORT=18768 docker compose -p cb-workflow-acceptance -f deploy/compose.test.yml -f deploy/compose.workflows-test.yml down -v
```

The overlay enables workflows, mounts the client read-only and reuses bounded
local-origin/CSRF/error handling. With an existing isolated stack, use its explicit
synthetic demo file and no competing stack. Cleanup affects only that named
project. Unit/configuration checks are not native acceptance; retain the actual
smoke/accounting result before claiming a pass.
