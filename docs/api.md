# API and implementation contracts

This is a target contract, not an inventory of shipped routes. The [runtime guide](runtime.md) is authoritative for implemented endpoints and limits. Optional API expansion is listed in the [roadmap](roadmap.md).

## Versioning and authorization

The initial application API is `/api/v1`. It uses JSON, strict unknown-field rejection, bounded bodies, request IDs, UTC timestamps, and UUID object IDs. Durations and usage use integer seconds. Wire numbers stay within `2^53 - 1`; the database uses checked BIGINT counters. The current [schemas](../contracts/README.md) define planning inputs; implementation must generate OpenAPI from runtime serializers and test compatibility with these contracts.

Project tokens authenticate machine clients. Human sessions authenticate approval and administration; all browser mutations require CSRF protection. Worker identities authenticate only the internal lease/report API. The server resolves actor and project from verified credentials, never from a caller-supplied actor field. A `project_id` in a body must match the credential's project, and every referenced object must be authorized. Object IDs, digests and native Slurm IDs are not bearer capabilities.

Unknown fields fail validation rather than being silently ignored. Content types are allowlisted. Duplicate JSON keys, nonfinite numbers, invalid UTF-8 and unpaired surrogates fail before hashing/validation. Implemented public JSON boundaries are normally 2 MiB. The `chunked-sha256/v1` data path supports 8 GiB per file and 64 GiB per job, at most 100 source files and 100 exact output paths; individual decoded chunks are at most 1 MiB. The inline object endpoint remains limited to 512 KiB. Installation administrators can narrow limits. See [large files and live output](data-transfer.md).

## Application endpoints

| Method and path | Authority | Result and behavior |
| --- | --- | --- |
| `GET /health/live` | No private information | Process liveness only |
| `GET /health/ready` | Internal/operator | Dependency readiness; no secrets, hosts or project details in public response |
| `GET /api/v1/projects/{id}/allowance` | Project read | Limits, spent, reserved, available, debt, policy version and outstanding uncertainty |
| `GET /api/v1/connections` | Project read | Assigned connections, safe capabilities, authentication state and current effective project/cluster resource limits |
| `POST /api/v1/connections/{id}/refresh` | Human connection owner/admin | Queue bounded discovery; agents read cached snapshots rather than flood schedulers |
| `POST /api/v1/projects/{id}/objects` | Upload scope | Stream one regular file; return immutable digest/size descriptor after verification |
| `POST /api/v1/projects/{id}/objects/lookup` | `objects:write` | Implemented bounded content lookup; at most 100 SHA-256/size pairs, only this project's authorized published objects, metadata-only existence hint; no source bytes or foreign existence disclosure |
| `POST /api/v1/projects/{id}/transfers` | `objects:write` | Reserve or resume a hash/size-bound upload; return transfer ID and received offset, or an already authorized complete receipt |
| `POST /api/v1/projects/{id}/transfers/{transfer}` | `objects:write` | Receive one offset/base64 chunk; `finish=true` verifies full size/SHA-256 before publishing and granting independent input authority |
| `POST /api/v1/jobs` | `jobs:submit` | Create immutable revision from the job-request schema; does not execute |
| `POST /api/v1/jobs/{id}/prepare` | `jobs:submit` | Queue preparation; return `202` with operation ID and job link |
| `GET /api/v1/jobs/{id}` | `jobs:read` | Preparation, decision, attempt and accounting states, safe explanation/actions |
| `GET /api/v1/jobs/{id}/completion` | `jobs:read`; artifacts need `artifacts:read` | Implemented provenance, paginated attempts, collection, ledger accounting and per-key delivery state |
| `GET/POST /api/v1/projects/{id}/investigations` | `jobs:read` / `investigations:write` | Optional implemented investigation list / finite plan creation |
| `GET /api/v1/investigations/{id}` | `jobs:read` | Optional grouped status, abandoned drafts and aggregate accounting |
| `POST /api/v1/investigations/{id}/jobs` | `investigations:write` | Attach explicit project jobs and roles |
| `POST /api/v1/investigations/{id}/finish` | `investigations:write`; cancellation needs `jobs:cancel` | Close scope/report, optionally cancel unstarted packages and reviews |
| `GET /api/v1/projects/{id}/jobs` | `jobs:read` | Implemented key-scoped recovery inbox, state filter and keyset pagination |
| `POST /api/v1/jobs/{id}/cancel-prepared` | `jobs:cancel` | Dismiss a never-submitted draft without deleting its evidence |
| `GET /api/v1/projects/{id}/availability` | `jobs:read` | Cached timestamped scheduler-visible CPU/GPU and queue observations |
| `POST /api/v1/projects/{id}/availability` | `jobs:read` | Request bounded read-only refreshes for permitted clusters; rate limited per cluster |
| `GET /api/v1/jobs/{id}/package` | `jobs:read` | Manifest, externally computed digest, exact script file links and maximum charge |
| `POST /api/v1/jobs/{id}/execution-requests` | `jobs:submit` | Enter review or attempt deterministic authorization under policy; return `202` and status |
| `POST /api/v1/jobs/{id}/decisions` | Human approver | Approve/reject exact revision+digest, with optional comment; never accepts project keys |
| `POST /api/v1/jobs/{id}/revisions` | `jobs:submit` | Create new revision; previous revision/decision retained, no inherited approval |
| `POST /api/v1/attempts/{id}/cancel` | `jobs:cancel` | Idempotent cancellation request; does not imply scheduler confirmation or refund |
| `POST /api/v1/jobs/{job}/execution-requests/{request}/cancel` | `jobs:cancel` | Release an unapproved request's hold, or request safe cancellation of its admitted attempt |
| `GET /api/v1/attempts/{id}/logs` | `jobs:read` | Bounded UTF-8-safe stdout/stderr slices, cursor and truncation flags |
| `POST /api/v1/attempts/{id}/acknowledge-results` | `jobs:read` | Acknowledge terminal results for this key; no scientific validation or accounting change |
| `POST /api/v1/attempts/{id}/collect` | `jobs:write` | Retry declared output collection after native completion, preserving execution and settled accounting |
| `GET /api/v1/attempts/{id}/artifacts` | `artifacts:read` | Authorized registered outputs, hashes, sizes, retention and download links |
| `GET /api/v1/artifacts/{id}/content` | `artifacts:read` | Project-authorized download/range; no arbitrary filesystem path |
| `POST /api/v1/projects/{id}/credentials` | Human project admin | Generate scoped token once; store only its hash, independent display identifier and metadata |
| `POST /api/v1/projects/{id}/credentials/{credential_id}/revoke` | Human project admin | Revoke future agent access; existing job reconciliation continues |
| `POST /api/v1/projects/{id}/grants` | Human project admin | New immutable policy/grant version and audit record; installations may impose ceilings |
| `POST /api/v1/projects/{id}/stop` | Human project admin | Pause admissions/dispatch and request cancellation of outstanding managed attempts |
| `POST /api/v1/connections/{id}/login` | Trusted human authentication channel | Start narrow protected MFA exchange for an enrolled connection; no remote shell |
| `POST /api/v1/connections/{id}/disable` | Human connection owner/admin | Reversible admission pause and close reusable session; preserve outstanding job records |
| `POST /api/v1/workers/{id}/revoke` | Human service admin | Revoke worker identity, fence future permits and quarantine/reconcile in-flight dispatch |

No agent API changes hostnames, SSH configuration, secret references, runtime images, workers, users, roles, approvals or allowance limits. Human policy management is exposed through explicit operations, not a generic database admin API. Django admin, if used, is operator-only and unavailable to project credentials.

Upload object references are project-owned. Reusing a digest from another project does not grant access or disclose its existence. Streaming verifies actual bytes/limits, not client-declared sizes. Reserve per-project/global storage capacity and object counts before accepting bytes; enforce concurrent-stream limits and disk headroom. Reconcile aborted uploads and expire orphan objects without deleting unsettled evidence. Deletion/retention is a separate administrative policy, not path-based recursive deletion.

A human decision binds an execution-request UUID as well as the immutable revision and digest. One approval permits one attempt in version one. Authorization atomically consumes it together with reservation/outbox creation. Repeating a completed idempotent execution request returns its existing result; a new execution request needs a fresh human approval or fresh budgeted admission. A previous decision is never a reusable unlimited permission for the package.

### Current authenticated project discovery

Job and investigation status accept `after_revision` and `wait_seconds` (0–20)
and return `revision` / `changed`. Investigation job creation can wrap the
canonical request as `{request: ..., context: {investigations: {id: ..., role: ...}}}`;
ungrouped jobs use canonical job bodies. Group filtering uses the bounded
JSON `context` query parameter. See [investigations and completion](investigations.md)
for scopes, advisory plan limits, waiting capacity and manifest consistency.

`GET /api/v1/context` returns `schema_version: "1.0"`, `project` (`id`, `name`) and `credential` (`id`, `scopes`, `execution_mode`). It requires a valid project bearer key and current issuing-user eligibility/project membership. Keys with `jobs:read` also receive `compute` (the current `/api/v1/connections` payload) and `allowance` (the project's current allowance response); other keys receive identity metadata only. Human browser sessions and worker keys do not authorize it. It returns only the key's own project and no token, token hash, SSH settings or private credential references; success responses use `Cache-Control: no-store` and vary on `Authorization`.

The stdio adapter uses this endpoint before starting MCP and derives routing identity from the verified key. `get_project_context` refreshes the same metadata. Job preparation/revision and workflow node requests may omit `project_id` through MCP; the adapter inserts it before calling the existing REST API. Direct REST job requests still require the canonical project ID, and all REST endpoints continue to enforce their own scopes/project checks. The authenticated key is the sole project selector; there is no separately configured project UUID.

Keys with `jobs:read` also receive `work_summary`: unfinished project jobs,
unread results and results awaiting acknowledgement, with the recovery tool name.
Compute targets include current availability cache freshness and queue visibility;
enabled environment inventory adds observed runtime summaries. See
[agent recovery and availability](agent-recovery.md) and
[environment builds](environment-builds.md) for the implemented routes, per-key
result tracking, scheduled Python inventory and immutable package updates.

Read current `effective_limits` from `list_compute_targets` before sizing work. Each target combines the project's per-job CPU/GPU/memory/walltime ceilings with the configured cluster ceilings and protocol bounds. `per_job_limits` contains the usable minima, `allocation_floors` describes CPU/GPU rounding, and `project_limits`/`cluster_limits` explain the inputs. `feasible`, `blocking_reasons` and `unknowns` report configurations that cannot fit even a minimal request or lack necessary information. An independent maximum does not prove that all maxima coexist on one node or that a selected partition/account/QoS accepts the job. These are configured ceilings, not live free-resource counts. `mixed` policy permits a separate human-review path above its automatic envelope while cluster caps and allowance still apply.

The connections payload also identifies `policy_revision`, project `per_job_limits`, `project_mode`, `project_paused`, `admission_expires_at`, `key_execution_mode`, `allowed_gpu_types` and `as_of`, alongside `connections` and cluster-scoped `environments`. A target reports `requires_human_approval` and its immutable `connection_revision_sha256`. Job detail includes `resource_validation` so a prepared package's effective allocation can be compared with current limits.

GPU permissions use explicit type checkboxes. None selected denies GPU jobs;
a partial selection requires `resources.gpu_type` to name an allowed type.
Selecting every discovered type across the project's allowed clusters records
`allow_untyped_gpus` in its policy. Each connection's `untyped_gpu_partitions`
reports where an agent may omit `gpu_type`, for example `"gpus": 1` renders
`--gres=gpu:1`. Admission and dispatch both recheck that current, complete node
discovery contains no unapproved types on that partition. Unknown/incomplete
discovery or newly discovered unapproved types block generic requests. Discovery
alone never upgrades a partial permission. Refresh discovery and explicitly save
the desired selection to update permissions. Resource ceilings, review and GPU
device-hour reservations still apply. A site's own Slurm policy may require an
explicit type even when the project permits generic requests.

Read `get_project_allowance` as well: available CPU/GPU seconds, debt, active jobs, lifetime submissions and expiry can block a job that fits the per-job ceilings. Reserve effective allocated resources multiplied by requested walltime. Final settlement charges actual allocated elapsed time in full, including usage beyond the reservation. No termination, overtime or cleanup interval is added to planning limits.

## Idempotency and errors

Execution requests awaiting approval immediately reserve their prepared CPU/GPU
resource-seconds in the same project transaction. Approval transfers the claim
to an attempt without reserving twice. Rejection or cancellation before dispatch
releases it; a job that may have reached the scheduler retains its reservation
until final accounting. Preparation alone creates no claim. Pending reviews do
not consume lifetime submission or active-job slots.

The allowance's `reserved` totals include waiting approvals and queued/running
attempts. `reserved_awaiting_approval` reports the pending subset, and
`jobs.awaiting_approval` reports its request count. Execution responses and job
details include `budget_reservation` with `state` (`none`, `held`, `converted` or
`released`), `cpu_core_seconds` and `gpu_device_seconds`.

`approval` reports `requires_human_approval`, `approval_reasons` and conditional
mixed-policy reasons. Every reason has a stable `code`, `source` and explanatory
`message`; sources are project, key or cluster. Execution requests retain the
requirements at request time, and job detail also reports current requirements.
Budgeted project mode does not override key- or cluster-required confirmation.
Policy changes do not automatically execute older pending requests. Their holds
can be cancelled explicitly using the route above (empty JSON body) or MCP
`cancel_execution_request(job_id, execution_request_id, idempotency_key)`.

Require `Idempotency-Key` on job creation, execution requests, decisions and grant changes. Scope it to project and operation (human/admin operations also bind actor as appropriate), and retain it for at least the operation lifetime. The key stores the canonical request digest and durable result. Reusing it with different bytes returns `409 IDEMPOTENCY_CONFLICT`. An HTTP retry and a new execution attempt are different actions.

Attempt/request cancellation and finished-output collection retry accept an empty
object and reuse their durable state transitions without a new charged retry
receipt. Repetition retains the same attempt; pending approval holds release
once, and uncertain dispatched allocations keep their accounting reservation.
These closeout operations remain available at metadata/receipt capacity. They
recheck ordinary authority and return current state rather than a cached reply.

Use an error envelope:

```json
{
  "error": {
    "code": "BUDGET_UNAVAILABLE",
    "message": "The project has insufficient unreserved GPU time.",
    "request_id": "55555555-5555-4555-8555-555555555555",
    "retryable": false,
    "details": {"dimension": "gpu_device_seconds", "required": 3600, "available": 1800}
  }
}
```

| HTTP status | Codes and use |
| --- | --- |
| `400` | `INVALID_JSON`, `INVALID_REQUEST`, `UNKNOWN_FIELD`, `UNSUPPORTED_FEATURE`, `RESOURCE_LIMIT`, unsafe logical path |
| `401` | Missing, expired or revoked credential; remote MCP also emits appropriate authentication metadata |
| `403` | `SCOPE_DENIED`, connection/environment disallowed, human authority required |
| `404` | Missing object or inaccessible cross-project reference without existence disclosure |
| `409` | `IDEMPOTENCY_CONFLICT`, `REVISION_CHANGED`, `POLICY_CHANGED`, invalid lifecycle transition, `BUDGET_UNAVAILABLE`, `PROJECT_PAUSED` |
| `413` | Request/file/package exceeds cap |
| `429` | `RATE_LIMITED`; project or installation burst exhausted, with retry seconds and `Retry-After` |
| `503` | Temporary service dependency failure; new admissions fail closed |

Login required, pending human review, and queued work normally appear as structured workflow states on a successful request. A later unknown submission returns a state/reconciliation explanation, not a misleading HTTP error inviting blind resubmission. Raw SSH exceptions, secrets, connection command lines and private paths are never passed through to agents.

`allowance.admission` describes outstanding job slots and separate creation and
execution token buckets for the project and installation. Keys share these
bounds. `409 BACKLOG_LIMIT` returns current usage/headroom and unknown retry
timing; completion or cancellation must free a slot. Retained history is bounded
by logical metadata bytes, independently of lifetime compute submissions.
`STORAGE_QUOTA` and `METADATA_QUOTA` identify the exhausted resource/scope and
include its usage, limit and remaining capacity; increasing CPU allowance does
not enlarge those quotas. See [runtime admission limits](runtime.md#burst-outstanding-work-and-retained-data-limits).

Resource rejections identify every exceeded dimension instead of requiring the agent to guess:

```json
{
  "error": {
    "code": "RESOURCE_LIMIT",
    "message": "Effective CPU allocation exceeds the project limit.",
    "details": {
      "violations": [
        {"resource": "cpu_cores", "requested": 3, "effective": 4,
         "limit": 2, "excess": 2, "source": "project", "unit": "cores"}
      ]
    }
  }
}
```

The four resources are `cpu_cores`, `gpu_devices`, `memory_mib` and `time_limit_seconds`; units are `cores`, `devices`, `MiB` and `seconds`. `effective` includes configured allocation rounding, and `source` identifies a project or cluster restriction. Limits are rechecked during preparation and admission, so previously read values may become stale. Refresh context, targets and allowance, then revise the resource request or choose another permitted target; do not repeatedly submit the same rejected package. The local client retains its stable `CONTROL_HTTP_ERROR` code/status and surfaces `broker_code: RESOURCE_LIMIT` with sanitized numeric `details`. It bounds error-body reads and discards remote prose and unrecognized fields before rendering the MCP tool error.

Recognized request, scheduler, inspection, input-authority and admission errors
also retain an allowlisted `broker_code`. The synchronous and asynchronous
clients render HTTP status and fixed local recovery guidance; they never display
the remote message or arbitrary details. Unknown codes or malformed/oversized
error bodies remain generic. For example, `NOT_PREPARED` directs the caller to
prepare first, `SCHEDULER_OPTIONS_REQUIRED` directs it to the permitted target
options, and `INPUT_DENIED` preserves the requirement for uploaded or authorized
artifact inputs. Failed generated probes are resumed through `inspect_environment`.

## MCP tools

Local stdio MCP is a facade under the agent account calling the authenticated service API. It does not import Django models, read the control database, or inherit an unrestricted SSH agent. The application API remains the policy authority even if the facade is modified.

| Tool | Scope | Behavior |
| --- | --- | --- |
| `get_project_context` | Valid project key and current membership | The key's project, scopes and execution-mode preference; planning limits/targets when `jobs:read` is present; no secret values |
| `list_compute_targets` | `jobs:read` | Assigned targets, capabilities and current `effective_limits` |
| `get_project_allowance` | `jobs:read` | Shared balance, reservations, current per-job policy and submission limits |
| `upload_job_file` | Upload scope | Bounded content upload; local bridge may read only configured local roots |
| `prepare_job` | `jobs:submit` | Create/prepare immutable revision, return review URL and required reservation |
| `request_execution` | `jobs:submit` | Request human review or deterministic budgeted admission |
| `get_job` | `jobs:read` | States, rejection comment, attempt identity and next actions |
| `get_job_logs` | `jobs:read` | Bounded log slices |
| `retry_job_outputs` | `jobs:write` | Recover a finished attempt's declared outputs without rerunning compute |
| `list_job_artifacts` | `artifacts:read` | Registered outputs only |
| `get_job_artifact` | `artifacts:read` | Bounded text/structured data or authorized download reference |
| `cancel_job_attempt` | `jobs:cancel` | Cancel one owned managed attempt |

Use correct read/write annotations, output schemas, bounded outputs and stable IDs. Do not expose `approve_job`, `increase_budget`, generic shell execution, arbitrary URLs, SSH options, or unrestricted file download. An upstream MCP server may contribute tool designs but is not placed directly in the agent's path with privileged credentials.

Tool calls return quickly with durable IDs. Neither HTTP nor stdio waits for an hours-long scheduler job. Completion notifications and agent resumption are separate optional orchestration, never assumed to be supplied by MCP alone. A remote HTTP MCP client requires a standards-compliant OAuth resource-server flow and cross-client conformance tests; browser OIDC login alone does not provide that protocol. [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)

## Internal worker API

Worker endpoints are on a private listener/Unix socket. Authentication establishes the worker identity and assigned connection set. Workers have no SQL/object-store administration credential.

| Operation | Request | Control checks and response |
| --- | --- | --- |
| `lease` | Worker capability version, capacity | Lease an eligible typed operation for an assigned connection, with generation, expiry and bounded inputs |
| `dispatch-confirm` | Attempt, nonce, generation, package digest, connection revision | Atomically validate live reservation/policy/connection/worker and record point of dispatch intent; one current permit |
| `observation` | Lease identity, native identity, timestamp, bounded structured status/evidence | Reject stale or conflicting reports; preserve raw evidence; never allow worker-supplied budget mutations |
| `heartbeat` | Worker/connection states and operation progress | Update liveness without treating absent heartbeat as job completion |
| `complete` | Lease result, evidence digests, known/unknown side-effect outcome | Complete typed operation; control decides reconciliation/settlement |

Initial execution authorizations travel on authenticated online channels. Optional signed envelopes are deferred until relayed connectors justify them; signing is not a replacement for online dispatch confirmation or replay protection. Cache no offline spending authority. A stale lease cannot trigger a second scheduler submission.

Human login/MFA uses a different narrow channel that can interact only with a configured connection's authentication helper. Do not multiplex a generic terminal, project API key or reusable SSH control socket into that channel.

## Job example

See [GPU request](../contracts/examples/gpu-job.json), [CPU request](../contracts/examples/cpu-job.json), [prepared GPU package](../contracts/examples/prepared-gpu-job.json), and [project policy](../contracts/examples/project-policy.json). The GPU example requests 8 cores and 1 A100 for 3,600 seconds, reserving 28,800 core-seconds and 3,600 GPU-seconds. Settlement uses actual observed allocated runtime, including any elapsed time beyond the reservation.

The example manifest uses synthetic file digests. Runtime tests must build real file bundles, compute canonical manifests, verify actual hashes and compare the final scheduler script against what the reviewer saw.

Content lookup checks the retained file's type, link count and declared size, without
reading its contents. It is an upload-deduplication hint, not fresh hash verification.
Publication and actual source/result consumption retain their content checks;
same-size corruption can therefore be reported by preparation or download after
a positive lookup. Missing or malformed retained files fail with a storage error.
