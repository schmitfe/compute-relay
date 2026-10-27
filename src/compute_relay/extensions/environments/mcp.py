"""Optional MCP tools expose recipes and jobs, never package-manager commands."""


def register(server, facade):
    from mcp.types import ToolAnnotations
    from compute_relay.mcp.facade import _uuid

    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

    @server.tool(annotations=readonly)
    def get_environment_inventory(environment_id: str) -> dict:
        """Read the actual observed Python version, installed packages and fingerprint, with observation time and managed-revision drift status. A registered name/version is not runtime proof. Missing/stale inventories can be refreshed by inspect_environment; Python is supported first."""
        return facade.client.request("GET", f"/api/v1/environments/{_uuid(environment_id)}/inventory")

    @server.tool(annotations=write)
    def inspect_environment(environment_id: str, resources: dict | None = None, slurm: dict | None = None,
                            idempotency_key: str | None = None) -> dict:
        """Request a bounded compute-node Python/package inventory as an ordinary prepared and approved/budgeted job. Resumes this key's pending probe; omitted resources retain its saved request. Sole configured partition/account/QOS choices are filled even with partial slurm options; select ambiguous choices from list_compute_targets. Retry a failed preparation here, with a new idempotency key if arguments change: corrections create an immutable job revision before execution is requested. Use this tool instead of revise_job for generated probe inputs. After execution is requested, poll get_environment_inventory or cancel the job before changing settings. Returns job/attempt or pending-review identity. No installer runs; requires jobs:write and jobs:execute."""
        data = {key: value for key, value in (("resources", resources), ("slurm", slurm)) if value is not None}
        return facade._mutate(f"/api/v1/environments/{_uuid(environment_id)}/inspect", data, idempotency_key)

    @server.tool(annotations=write)
    def request_environment_packages(environment_id: str, packages: list[dict], name: str | None = None,
                                     resources: dict | None = None, slurm: dict | None = None,
                                     idempotency_key: str | None = None) -> dict:
        """Build a new usable Python environment or update a managed one through a new immutable revision. packages are uploaded wheel descriptors {name,version,filename,sha256,size_bytes}; include a closed pinned dependency set. Existing managed requirements are retained except names explicitly replaced. This prepares and requests an ordinary compute job. Project policy may auto-permit verified results; otherwise promotion needs a human. The old prefix/revision remains unchanged."""
        data = {"packages": packages, **{key: value for key, value in (("name", name), ("resources", resources), ("slurm", slurm)) if value is not None}}
        return facade._mutate(f"/api/v1/environments/{_uuid(environment_id)}/packages", data, idempotency_key)

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
        """Read build/provenance, observed runtime inventory and the published environment reference. permitted_for_jobs reflects the project's automatic-use policy or explicit human promotion."""
        return facade.client.request("GET", f"/api/v1/environment-builds/{_uuid(build_id)}")
