# API and implementation contracts

This is the target API contract, including future management endpoints and protocol limits. The [runtime guide](runtime.md) lists the implemented alpha endpoints and its narrower upload/storage limits. OpenAPI generation, request IDs, and the complete administration API remain planned.

## Versioning and authorization

The initial application API is `/api/v1`. It uses JSON, strict unknown-field rejection, bounded bodies, request IDs, UTC timestamps, and UUID object IDs. Durations and usage use integer seconds. Wire numbers stay within `2^53 - 1`; the database uses checked BIGINT counters. The current [schemas](../contracts/README.md) define planning inputs; implementation must generate OpenAPI from runtime serializers and test compatibility with these contracts.

Project tokens authenticate machine clients. Human sessions authenticate approval and administration; all browser mutations require CSRF protection. Worker identities authenticate only the internal lease/report API. The server resolves actor and project from verified credentials, never from a caller-supplied actor field. A `project_id` in a body must match the credential's project, and every referenced object must be authorized. Object IDs, digests and native Slurm IDs are not bearer capabilities.

Unknown fields fail validation rather than being silently ignored. Content types are allowlisted. Duplicate JSON keys, nonfinite numbers, invalid UTF-8 and unpaired surrogates fail before hashing/validation. Default request JSON cap: 1 MiB. Files upload individually with a 100 MiB per-file and 256 MiB per-package cap, at most 100 source files and 100 exact output paths. Installation administrators can narrow limits; expanding the protocol caps requires a versioned change and tests.

## Application endpoints

| Method and path | Authority | Result and behavior |
| --- | --- | --- |
| `GET /health/live` | No private information | Process liveness only |
| `GET /health/ready` | Internal/operator | Dependency readiness; no secrets, hosts or project details in public response |
| `GET /api/v1/projects/{id}/allowance` | Project read | Limits, spent, reserved, available, debt, policy version and outstanding uncertainty |
| `GET /api/v1/connections` | Project read | Assigned connections, safe capabilities, authentication state and current effective project/cluster resource limits |
| `POST /api/v1/connections/{id}/refresh` | Human connection owner/admin | Queue bounded discovery; agents read cached snapshots rather than flood schedulers |
| `POST /api/v1/projects/{id}/objects` | Upload scope | Stream one regular file; return immutable digest/size descriptor after verification |
| `POST /api/v1/jobs` | `jobs:submit` | Create immutable revision from the job-request schema; does not execute |
| `POST /api/v1/jobs/{id}/prepare` | `jobs:submit` | Queue preparation; return `202` with operation ID and job link |
| `GET /api/v1/jobs/{id}` | `jobs:read` | Preparation, decision, attempt and accounting states, safe explanation/actions |
| `GET /api/v1/jobs/{id}/package` | `jobs:read` | Manifest, externally computed digest, exact script file links and maximum charge |
| `POST /api/v1/jobs/{id}/execution-requests` | `jobs:submit` | Enter review or attempt deterministic authorization under policy; return `202` and status |
| `POST /api/v1/jobs/{id}/decisions` | Human approver | Approve/reject exact revision+digest, with optional comment; never accepts project keys |
| `POST /api/v1/jobs/{id}/revisions` | `jobs:submit` | Create new revision; previous revision/decision retained, no inherited approval |
| `POST /api/v1/attempts/{id}/cancel` | `jobs:cancel` | Idempotent cancellation request; does not imply scheduler confirmation or refund |
| `GET /api/v1/attempts/{id}/logs` | `jobs:read` | Bounded UTF-8-safe stdout/stderr slices, cursor and truncation flags |
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

`GET /api/v1/context` returns `schema_version: "1.0"`, `project` (`id`, `name`) and `credential` (`id`, `scopes`, `execution_mode`). It requires a valid project bearer key and current issuing-user eligibility/project membership. Keys with `jobs:read` also receive `compute` (the current `/api/v1/connections` payload) and `allowance` (the project's current allowance response); other keys receive identity metadata only. Human browser sessions and worker keys do not authorize it. It returns only the key's own project and no token, token hash, SSH settings or private credential references; success responses use `Cache-Control: no-store` and vary on `Authorization`.

The stdio adapter uses this endpoint before starting MCP and derives routing identity from the verified key. `get_project_context` refreshes the same metadata. Job preparation/revision and workflow node requests may omit `project_id` through MCP; the adapter inserts it before calling the existing REST API. Direct REST job requests still require the canonical project ID, and all REST endpoints continue to enforce their own scopes/project checks. Optional legacy `BROKER_PROJECT_ID` must match the discovered identity or adapter startup fails.

Read current `effective_limits` from `list_compute_targets` before sizing work. Each target combines the project's per-job CPU/GPU/memory/walltime ceilings with the configured cluster ceilings and protocol bounds. `per_job_limits` contains the usable minima, `allocation_floors` describes CPU/GPU rounding, and `project_limits`/`cluster_limits` explain the inputs. `feasible`, `blocking_reasons` and `unknowns` report configurations that cannot fit even a minimal request or lack necessary information. An independent maximum does not prove that all maxima coexist on one node or that a selected partition/account/QoS accepts the job. These are configured ceilings, not live free-resource counts. `mixed` policy permits a separate human-review path above its automatic envelope while cluster caps and allowance still apply.

The connections payload also identifies `policy_revision`, project `per_job_limits`, `project_mode`, `project_paused`, `admission_expires_at`, `key_execution_mode`, `allowed_gpu_types` and `as_of`, alongside `connections` and cluster-scoped `environments`. A target reports `requires_human_approval` and its immutable `connection_revision_sha256`. Job detail includes `resource_validation` so a prepared package's effective allocation can be compared with current limits.

Read `get_project_allowance` as well: available CPU/GPU seconds, debt, active jobs, lifetime submissions and expiry can block a job that fits the per-job ceilings. Reserve effective allocated resources multiplied by requested walltime. Final settlement charges actual allocated elapsed time in full, including usage beyond the reservation. No termination, overtime or cleanup interval is added to planning limits.

## Idempotency and errors

Require `Idempotency-Key` on job creation, execution requests, decisions, grant changes and cancellation. Scope it to project and operation (human/admin operations also bind actor as appropriate), and retain it for at least the operation lifetime. The key stores the canonical request digest and durable result. Reusing it with different bytes returns `409 IDEMPOTENCY_CONFLICT`. An HTTP retry and a new execution attempt are different actions.

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
| `429` | Bounded admission/API rate limit, with retry interval |
| `503` | Temporary service dependency failure; new admissions fail closed |

Login required, pending human review, and queued work normally appear as structured workflow states on a successful request. A later unknown submission returns a state/reconciliation explanation, not a misleading HTTP error inviting blind resubmission. Raw SSH exceptions, secrets, connection command lines and private paths are never passed through to agents.

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
| `list_job_artifacts` | `artifacts:read` | Registered outputs only |
| `get_job_artifact` | `artifacts:read` | Bounded text/structured data or authorized download reference |
| `cancel_job_attempt` | `jobs:cancel` | Cancel one owned managed attempt |

Use correct read/write annotations, output schemas, bounded outputs and stable IDs. Do not expose `approve_job`, `increase_budget`, generic shell execution, arbitrary URLs, SSH options, or unrestricted file download. An upstream MCP server may contribute tool designs but is not placed directly in the agent's path with privileged credentials.

Tool calls return quickly with durable IDs. Neither HTTP nor stdio waits for an hours-long scheduler job. Completion notifications and agent resumption are separate optional orchestration, never assumed to be supplied by MCP alone. Hosted HTTP MCP adds a standards-compliant OAuth resource-server flow and cross-client conformance tests; browser OIDC login does not automatically implement that protocol. [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)

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
