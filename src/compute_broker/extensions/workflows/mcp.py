from uuid import UUID
from mcp.types import ToolAnnotations


def register(server, facade):
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

    @server.tool(annotations=write)
    def prepare_workflow(document: dict, idempotency_key: str | None = None) -> dict:
        """Seal at most 32 job nodes and success-only artifact edges. Omit project_id in node requests; executes nothing."""
        document = {**document, "nodes": [{**node, "request": facade.project_request(node["request"])}
                                          for node in document["nodes"]]}
        return facade._mutate(f"/api/v1/projects/{facade.project_id}/workflow-revisions", document, idempotency_key)

    @server.tool(annotations=write)
    def start_workflow(revision_id: str, idempotency_key: str | None = None) -> dict:
        """Start a bounded run; each ready job needs normal budget admission or human review."""
        return facade._mutate(f"/api/v1/workflow-revisions/{UUID(revision_id)}/runs", {}, idempotency_key)

    @server.tool(annotations=read)
    def get_workflow(run_id: str) -> dict:
        """Read workflow nodes, concrete job IDs, blocked inputs and errors."""
        return facade.client.request("GET", f"/api/v1/workflow-runs/{UUID(run_id)}")

    @server.tool(annotations=write)
    def advance_workflow(run_id: str) -> dict:
        """Recheck and materialize at most two ready nodes; existing attempts are reused."""
        return facade._mutate(f"/api/v1/workflow-runs/{UUID(run_id)}/advance", {})

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
    def cancel_workflow(run_id: str, idempotency_key: str | None = None) -> dict:
        """Stop future nodes and request ordinary cancellation of admitted attempts."""
        return facade._mutate(f"/api/v1/workflow-runs/{UUID(run_id)}/cancel", {}, idempotency_key)
