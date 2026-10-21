"""Optional MCP tools expose recipes and jobs, never package-manager commands."""


def register(server, facade):
    from mcp.types import ToolAnnotations
    from compute_broker.mcp.facade import _uuid

    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

    @server.tool(annotations=readonly)
    def get_environment_build_policy() -> dict:
        """Read explicitly enabled builders, bootstrap environments, resource/storage limits and target platform."""
        return facade.client.request("GET", f"/api/v1/projects/{facade.project_id}/environment-build-policy")

    @server.tool(annotations=write)
    def create_environment_build(recipe: dict, idempotency_key: str | None = None) -> dict:
        """Create a pinned environment recipe; installation runs only later as an approved/budgeted compute job. Only venv uploaded wheels are implemented."""
        return facade._mutate(f"/api/v1/projects/{facade.project_id}/environment-builds", recipe, idempotency_key)

    @server.tool(annotations=write)
    def prepare_environment_build(build_id: str, idempotency_key: str | None = None) -> dict:
        """Render the immutable build script/package for review without running an installer."""
        return facade._mutate(f"/api/v1/environment-builds/{_uuid(build_id)}/prepare", {}, idempotency_key)

    @server.tool(annotations=write)
    def request_environment_build(build_id: str, idempotency_key: str | None = None) -> dict:
        """Request normal project approval or budgeted execution; builds consume compute allowance and submission slots."""
        return facade._mutate(f"/api/v1/environment-builds/{_uuid(build_id)}/execution-requests", {}, idempotency_key)

    @server.tool(annotations=readonly)
    def get_environment_build(build_id: str) -> dict:
        """Read build/provenance status and the published environment reference. A human must explicitly permit a ready environment in project policy."""
        return facade.client.request("GET", f"/api/v1/environment-builds/{_uuid(build_id)}")
