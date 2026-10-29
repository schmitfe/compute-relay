# Python orchestration in a science workspace

An agent with local Python execution can stage files, prepare and request jobs,
wait for status changes and fetch verified results in one script. This avoids
returning every intermediate response through the model. The script still makes
ordinary HTTP requests; scheduler queueing, human review and collection take the
same time. No token saving or throughput improvement is assumed without measuring
the actual agent workflow.

The public imports reuse the existing implementation used by the MCP adapter:

```python
from compute_relay.sdk import ControlError, ProjectFacade, WorkspaceClient, finish

workspace = WorkspaceClient("/absolute/path/to/initialized-science-project")
project = ProjectFacade.from_credential(workspace.client)
context = project.get_project_context()
```

Initialize the workspace with `compute-relay init-mcp` first. The SDK reads its
private `.compute-relay/connection.json` and `.compute-relay/project.key`, verifies
the workspace binding and derives project identity from the authenticated broker
context. It does not require a project UUID or credentials embedded in scripts.
Run scripts with the installed interpreter recorded in `.compute-relay/USAGE.md`;
the science project's own Python environment need not contain compute-relay.

The generated guide contains a complete bounded preparation, execution, wait and
verified-export example. Existing guides are deliberately never overwritten:
review and preserve any custom edits, remove only `.compute-relay/USAGE.md`, then
rerun initialization with `--reuse-key` and the original broker URL and client
selection. This retains the existing credential. Do not remove the private
directory, connection metadata or key to refresh the guide.

## Reuse the existing operations

| Operation | Supported call |
| --- | --- |
| Upload only explicit source paths | `workspace.stage(["main.py", "src/util.py"])` |
| Prepare the saved bundle and a request without `files` or `project_id` | `workspace.prepare(bundle_id, "job.json", idempotency_key=None)` |
| Request the first execution of that immutable job | `project.request_execution(job_id)` |
| Read compact status, or wait for a revision change | `project.get_job(job_id, after_revision=revision, wait_seconds=20)` |
| Read allowance before planning a burst | `project.get_project_allowance()` |
| Recover jobs after reconnecting | `project.list_project_jobs(state="unfinished")` |
| Retry output collection without rerunning compute | `project.retry_job_outputs(attempt_id)` |
| Fetch into a fresh directory, or resume matching partial files | `workspace.fetch(attempt_id, "results/RUN", resume=True)` |
| Export provenance and verify retained outputs | `finish(workspace, job_id=job_id, output="results/RUN", resume=True)` |

An unchanged `workspace.prepare` derives a stable key from the request and source
hashes. The default execution key reuses that immutable job's first execution
request. After an interrupted response, recover the existing job; a fresh
execution key explicitly requests another execution. Stable identity remains
necessary when a script is restarted.

Give each script a finite job count and waiting deadline. Positive waits require
the previous snapshot's revision and are limited to 20 seconds. Only two server
wait slots exist: back off on `ControlError.broker_code == "WAIT_BUSY"`, rather
than opening a long wait for every job in a large batch. For an enabled
investigation, `project.get_investigation(id, after_revision=revision,
wait_seconds=20)` can wait on grouped progress. Poll remaining jobs without waits
or use a modest interval. The existing optional workflow module can represent a
bounded DAG with server-enforced dependencies through MCP; the SDK does not add a
second workflow engine or optional-module wrappers.

## Authority and completion

The script runs on the agent's workspace device. No Python execution endpoint is
added to the broker, MCP server or cluster connector. HTTP endpoints continue to
enforce project/key scopes, review, resource limits and spending admission. Client
approval of a local script is distinct from broker approval of compute; a script
cannot approve its own execution request. Respect the local agent client's
execution permissions as well.

Return a concise receipt for human review, rejected requests or failed output
collection. On local timeout or process exit, admitted jobs remain durable and
continue independently; stopping a wait does not cancel them. Use the ordinary
cancellation APIs only when cancellation is intended. Do not acknowledge results
until they have been assessed: `finish` defaults to `acknowledge=False`, and byte
integrity alone does not prove scientific validity. Keep credential contents,
source bytes and large intermediate responses out of chat.

This pattern follows the separation between tool protocol and agent orchestration
in the [MCP tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).
Anthropic describes the context-saving benefits of loops and filtering in a local
execution environment in [Code execution with MCP](https://www.anthropic.com/research/code-execution-with-mcp).
Those benefits depend on the client and workload; they are not a guarantee that
MCP itself requires a model round trip for every request.
