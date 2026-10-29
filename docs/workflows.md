# Optional workflows

Set `BROKER_EXTENSIONS=workflows` on the control service and enable `workflows`
in each participating worker's protected configuration, then migrate. The MCP
process must also explicitly enable the extension to expose its tools. The
production default is an empty extension list. See [the extension contract](extensions.md).

This extension stores immutable, hashed workflow revisions and durable runs in
its own Django app. A revision contains a name and at most 32 named nodes. Each
node contains an ordinary job request, optional `depends_on` node names, and
optional `inputs` edges with `node`, `output`, and destination `path` fields.
Only success dependencies are supported. Graphs must be acyclic; inputs must
refer to explicitly declared producer outputs and cannot overwrite consumer
code or other inputs.

Branching and joining are supported: `start → [left, right] → join` is a valid
diamond DAG. Set `depends_on: ["start"]` on both branches and
`depends_on: ["left", "right"]` on the join. The join waits for both branches
to succeed and settle their accounting; artifact edges additionally bind both
producers' verified outputs. These dependencies are orchestrated by Compute
Relay, rather than submitted as native Slurm dependency flags.

Outputs are currently collected after each job finishes. To analyze completed
chunks while other trials continue, use one job per chunk and connect the
analysis nodes to their corresponding chunk jobs. A growing file from one
still-running job is not yet exposed as an artifact; live stdout/stderr remains
available. Streaming immutable chunks is tracked in the roadmap.

The `workflows:write` scope supplements ordinary job scopes. Creating a revision
requires `jobs:write`; starting and advancing require `jobs:execute`; artifact
edges also require `artifacts:read`; cancellation requires `jobs:cancel`.
Cluster and environment permissions are checked again when nodes become ready.
Runs keep their originating API key. Revoking it stops new nodes; another key
cannot silently take over the run. Already submitted work remains observable
and accountable by the core worker even with this extension disabled.

Each ready node becomes one normal prepared job, with the same review page,
resource envelope, shared compute allowance, active limit and lifetime limit as
independent jobs. Approval-required keys create a separate pending review for
each node. No DAG-wide approval authorizes later scripts. A rejected node keeps
its comment in its job record; descendants block. Create a revised workflow to
change a script or rejected request. There are no automatic retries.

Producer jobs must have trustworthy terminal accounting before consumers can
advance. Edges bind retained artifact IDs, SHA-256 hashes and the exact producer
attempt. The consumer package includes those immutable bytes through the
existing broker relay on the same cluster. Cross-cluster artifact edges are
rejected until a separate transfer capability and policy exist. A missing
output blocks the consumer even if Slurm
reports success. This does not move files between arbitrary remote paths or
submit native scheduler dependencies. Large remote datasets and direct
transfers need a separately enabled transfer extension; it is not implemented.

The worker lease endpoint advances at most two eligible nodes per run for at
most four due runs. Due runs rotate and are filtered by participating worker,
so waiting reviews do not occupy the front of the queue forever. A two-minute
preparation claim allows recovery after a broker crash. Preparation and blob
publication happen outside the metadata transaction; node admission uses the
ordinary serialized project ledger. Status GETs never admit jobs.

## API and portal

| Action | Endpoint |
| --- | --- |
| Seal graph | `POST /api/v1/projects/{id}/workflow-revisions` |
| Start run | `POST /api/v1/workflow-revisions/{id}/runs` |
| Read status | `GET /api/v1/workflow-runs/{id}` |
| Advance ready nodes | `POST /api/v1/workflow-runs/{id}/advance` |
| Cancel run | `POST /api/v1/workflow-runs/{id}/cancel` |
| Human list | `/projects/{id}/workflows/` |
| Human run review | `/workflows/{id}/` |

Creation, start and cancellation support ordinary idempotency keys. Advance is
durably idempotent for a node's job and execution request. MCP tools are
`prepare_workflow`, `start_workflow`, `get_workflow`, `advance_workflow`, and
`cancel_workflow`. A project link appears only when the app is enabled.

Tests cover review/rejection, byte-bound artifact edges, accounting gates,
missing outputs, cap enforcement, revoked keys, duplicate generic execution
requests, crash recovery, cancellation races, scheduler fairness and HTTP
idempotency. The science acceptance kit includes an artifact-dependent case;
fresh third-party client acceptance is recorded separately from unit tests.
