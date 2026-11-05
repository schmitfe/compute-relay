# Python orchestration in a science workspace

Run bounded staging/preparation/status/export loops locally using the same
project credential and operations as MCP. Normal HTTP, scheduler and human-review
waits still apply; the broker exposes no Python execution endpoint.

```python
from compute_relay.sdk import ControlError, ProjectFacade, WorkspaceClient, finish

workspace = WorkspaceClient("/absolute/path/to/initialized-science-project")
project = ProjectFacade.from_credential(workspace.client)
context = project.get_project_context()
```

[Initialize the workspace](local-client-setup.md) first and use the installed
interpreter from `.compute-relay/USAGE.md`. The SDK validates its private workspace
binding/key and obtains project identity from authenticated context; embed no
credentials or project UUID in scripts.

Context supplies scopes, targets, allowance and recovery. Preparation needs
`jobs:write`, execution `jobs:execute`, upload `objects:write`, downloads
`artifacts:read`. Scopes never override policy. Target `effective_limits` exposes
`per_job_limits`, `allocation_floors`, `feasible`, `blocking_reasons` and `unknowns`.
Independent configured maxima need not coexist on a node and are not idle capacity.
Check allowance too: funds, expiry and active/lifetime slots can block admission.

## Operations

| Operation | Call |
| --- | --- |
| Stage explicit files | `workspace.stage(["main.py", "src/util.py"])` |
| Prepare a request without `files`/`project_id` | `workspace.prepare(bundle_id, "job.json", idempotency_key=None)` |
| Request first execution | `project.request_execution(job_id)` |
| Read/wait for status | `project.get_job(job_id, after_revision=revision, wait_seconds=20)` |
| Refresh allowance | `project.get_project_allowance()` |
| Recover existing work | `project.list_project_jobs(state="unfinished")` |
| Retry collection, never compute | `project.retry_job_outputs(attempt_id)` |
| Fetch/resume verified results | `workspace.fetch(attempt_id, "results/RUN", resume=True)` |
| Export provenance/results | `finish(workspace, job_id=job_id, output="results/RUN", resume=True)` |

Preparation derives a stable key from unchanged inputs; default execution reuses
that job's first request. A fresh explicit execution key deliberately requests
another run. Recover existing identities after interrupted replies. Give loops a
finite job count/deadline and back off on `WAIT_BUSY` rather than long-waiting on
every job. [Completion](investigations.md) defines waits and export consistency.

Lookup reuses only project-authorized objects. It checks retained type/size/links,
not fresh hashes; same-size corruption may fail later preparation/download checks.
HTTP failures expose `ControlError.code == "CONTROL_HTTP_ERROR"`, `status`, and
recognized `broker_code`/fixed guidance. `RESOURCE_LIMIT` adds validated numeric
`details["violations"]` (resource, requested/effective, limit, excess, source/unit).
Unknown/malformed errors stay generic; arbitrary remote prose is discarded.
Refresh targets/allowance and revise rejected resources before retrying.

## ZIP helpers

No initialized workspace is needed for these local operations:

```python
from compute_relay.sdk import pack_zip, unpack_zip, write_archive_helper

pack_zip("/absolute/path/to/science", ["src/model.py", "data/input.csv"], "source.zip")
write_archive_helper("/absolute/path/to/science", "relay-files.pyz")
unpack_zip("/absolute/path/to/science", "results/RUN/result.zip", "results/extracted",
           max_files=1000, max_bytes=1024**3)
```

Stage the helper/archive with the reviewed entrypoint for compute-node use.
[Archive boundaries](data-transfer.md#zip-helpers-for-local-files-and-compute-jobs)
define fresh destinations, path validation and size checks.

## Authority and completion

Respect client permission to run a local script separately from broker compute
approval. Return for human review, failed collection or
`next_action.action == "reconnect_connector"`; never request replacement compute
to escape a blocker. Local timeout/exit stops waiting, not accepted Slurm jobs.
`finish` defaults to `acknowledge=False`: assess results before acknowledgement;
verified bytes alone do not validate science. Return output locations, usage,
remaining reservations and unresolved states without exposing secrets/file bytes.
