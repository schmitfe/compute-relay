"""Optional read-only MCP tools; project credentials never manage storage roots."""

from .planning import validate_plan


def register(server, facade):
    from mcp.types import ToolAnnotations

    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

    @server.tool(annotations=readonly)
    def get_project_storage() -> dict:
        """Read approved target IDs, advisory budgets and cached capacity/unknowns. Does not probe or scan storage."""
        return facade.client.request("GET", f"/api/v1/projects/{facade.project_id}/storage")

    @server.tool(annotations=readonly)
    def plan_storage(request: dict) -> dict:
        """Dry-run declared peak byte/inode growth against selected targets. Creates no jobs, files, or reservations; does not enforce OS quotas."""
        return facade.client.request("POST", f"/api/v1/projects/{facade.project_id}/storage/plan", validate_plan(request))
