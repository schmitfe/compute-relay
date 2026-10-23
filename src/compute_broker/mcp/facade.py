"""Official SDK stdio server forwarding a project's bounded API operations."""

from __future__ import annotations

import base64
import os
from urllib.parse import urlencode
from uuid import UUID, uuid4

from compute_broker.worker.client import JsonClient, environment_token


def _uuid(value: str) -> str:
    return str(UUID(value))


class ProjectFacade:
    def __init__(self, client, project_id: str, enabled_extensions=()):
        self.client = client
        self.project_id = _uuid(project_id)
        from compute_broker.extensions import configured_names
        self.enabled_extensions = configured_names(",".join(enabled_extensions))

    @classmethod
    def from_credential(cls, client, *, expected_project_id=None, enabled_extensions=()):
        context = client.request("GET", "/api/v1/context")
        project_id = _uuid(context["project"]["id"])
        if expected_project_id is not None and _uuid(expected_project_id) != project_id:
            raise ValueError("configured project does not match the API key's project")
        return cls(client, project_id, enabled_extensions)

    def get_project_context(self):
        context = self.client.request("GET", "/api/v1/context")
        if _uuid(context["project"]["id"]) != self.project_id:
            raise ValueError("API key project changed; reconnect the MCP adapter")
        return context

    def project_request(self, request: dict):
        """Fill internal routing identity without changing the caller's document."""
        if "project_id" in request and request["project_id"] != self.project_id:
            raise ValueError("job must belong to the API key's project")
        return {**request, "project_id": self.project_id}

    def get_broker_capabilities(self):
        return self.client.request("GET", "/api/v1/capabilities")

    def _mutate(self, path, body, key=None):
        return self.client.request("POST", path, body,
                                   idempotency_key=key or str(uuid4()))

    def list_compute_targets(self):
        return self.client.request("GET", "/api/v1/connections")

    def get_project_allowance(self):
        return self.client.request("GET", f"/api/v1/projects/{self.project_id}/allowance")

    def upload_job_file(self, path: str, content_base64: str):
        # The facade never opens a caller-supplied local path or reads a URL.
        if len(content_base64) > 700_000:
            raise ValueError("MCP demo uploads are limited to 512 KiB")
        decoded = base64.b64decode(content_base64, validate=True)
        if len(decoded) > 512 * 1024:
            raise ValueError("MCP demo uploads are limited to 512 KiB")
        return self._mutate(f"/api/v1/projects/{self.project_id}/objects",
                            {"path": path, "content_base64": content_base64})

    def prepare_job(self, request: dict, idempotency_key: str | None = None):
        request = self.project_request(request)
        key = idempotency_key or str(uuid4())
        created = self._mutate("/api/v1/jobs", request, key)
        job_id = _uuid(created["id"])
        return self._mutate(f"/api/v1/jobs/{job_id}/prepare", {}, key + ":prepare")

    def request_execution(self, job_id: str, idempotency_key: str | None = None):
        return self._mutate(f"/api/v1/jobs/{_uuid(job_id)}/execution-requests", {}, idempotency_key)

    def revise_job(self, job_id: str, request: dict, idempotency_key: str | None = None):
        request = self.project_request(request)
        key = idempotency_key or str(uuid4())
        created = self._mutate(f"/api/v1/jobs/{_uuid(job_id)}/revisions", request, key)
        return self._mutate(f"/api/v1/jobs/{_uuid(created['id'])}/prepare", {}, key + ":prepare")

    def get_job(self, job_id: str):
        return self.client.request("GET", f"/api/v1/jobs/{_uuid(job_id)}")

    def get_job_logs(self, attempt_id: str, stream: str = "stdout", cursor: int = 0,
                     limit: int = 65536):
        if stream not in {"stdout", "stderr"} or not 0 <= cursor <= 2**53 - 1 or not 1 <= limit <= 65536:
            raise ValueError("invalid log request")
        query = urlencode({"stream": stream, "cursor": cursor, "limit": limit})
        return self.client.request("GET", f"/api/v1/attempts/{_uuid(attempt_id)}/logs?{query}")

    def list_job_artifacts(self, attempt_id: str):
        return self.client.request("GET", f"/api/v1/attempts/{_uuid(attempt_id)}/artifacts")

    def get_job_artifact(self, artifact_id: str, max_bytes: int = 65536):
        if not 1 <= max_bytes <= 1024 * 1024:
            raise ValueError("artifact response limit must be at most 1 MiB")
        content, media_type = self.client.get_bytes(
            f"/api/v1/artifacts/{_uuid(artifact_id)}/content", max_bytes)
        return {"artifact_id": _uuid(artifact_id), "media_type": media_type,
                "size_bytes": len(content), "content_base64": base64.b64encode(content).decode("ascii")}

    def cancel_job_attempt(self, attempt_id: str, idempotency_key: str | None = None):
        return self._mutate(f"/api/v1/attempts/{_uuid(attempt_id)}/cancel", {}, idempotency_key)

    def cancel_execution_request(self, job_id: str, execution_request_id: str, idempotency_key: str | None = None):
        return self._mutate(f"/api/v1/jobs/{_uuid(job_id)}/execution-requests/{_uuid(execution_request_id)}/cancel",
                            {}, idempotency_key)


def create_server(facade: ProjectFacade):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("Compute Broker", instructions=(
        "Before preparing work, read get_project_context, list_compute_targets and "
        "get_project_allowance. Use each target's effective_limits, which combine the "
        "project and cluster limits, and account for allocation rounding. Reserve against "
        "the requested walltime. These limits can change; saved cluster capacity alone is not permission "
        "to use it. If RESOURCE_LIMIT is returned, inspect the reported effective values "
        "and limits, refresh those tools, and revise the resource request or choose "
        "another permitted target. Do not repeatedly submit the same rejected request. "
        "Pass a job document as prepare_job.request or revise_job.request; omit project_id "
        "because the API key selects the project. Preparation does not execute code. "
        "Execution requests awaiting approval hold budget, as do queued and running jobs. "
        "Read approval.approval_reasons: project, API key and cluster policies can each require review. "
        "Cancel an unwanted pending request with cancel_execution_request to release its hold. "
        "Changing project policy does not automatically execute older pending requests."
    ))
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                               idempotentHint=True, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                            idempotentHint=False, openWorldHint=True)

    @server.tool(annotations=readonly)
    def get_project_context() -> dict:
        """Read this API key's project, active limits, scopes and execution preference. Requires no project ID; never returns a secret. Refresh before planning work."""
        return facade.get_project_context()

    @server.tool(annotations=readonly)
    def get_broker_capabilities() -> dict:
        """Read server-enabled extensions and this key's scopes before planning work."""
        return facade.get_broker_capabilities()

    @server.tool(annotations=readonly)
    def list_compute_targets() -> dict:
        """Read permitted clusters, capabilities and effective_limits (project/cluster minimums). Use allocation rounding when sizing jobs; reservations use requested walltime. Does not log in to a cluster."""
        return facade.list_compute_targets()

    @server.tool(annotations=readonly)
    def get_project_allowance() -> dict:
        """Read current project resource limits, shared funds, reservations and uncertainty. Combine these with each target's effective_limits before preparing work."""
        return facade.get_project_allowance()

    @server.tool(annotations=write)
    def upload_job_file(path: str, content_base64: str) -> dict:
        """Upload one base64-encoded source file (512 KiB maximum); path is a logical name."""
        return facade.upload_job_file(path, content_base64)

    @server.tool(annotations=write)
    def prepare_job(request: dict, idempotency_key: str | None = None) -> dict:
        """Prepare a job for review after checking current target effective_limits and allowance. Omit project_id; the API key selects the project. Set request.name to a descriptive 1–80 character job name (letters, digits, _, ., -). On RESOURCE_LIMIT, revise the resources before retrying. Preparation never executes code."""
        return facade.prepare_job(request, idempotency_key)

    @server.tool(annotations=write)
    def revise_job(job_id: str, request: dict, idempotency_key: str | None = None) -> dict:
        """Prepare a linked revision after review feedback. Omit project_id; changes need a new execution request."""
        return facade.revise_job(job_id, request, idempotency_key)

    @server.tool(annotations=write)
    def request_execution(job_id: str, idempotency_key: str | None = None) -> dict:
        """Request approval or policy-authorized execution and hold its budget. Returns state, approval reasons and reservation. Reuse the idempotency key on retries."""
        return facade.request_execution(job_id, idempotency_key)

    @server.tool(annotations=readonly)
    def get_job(job_id: str) -> dict:
        """Read a project's job status and human rejection comments."""
        return facade.get_job(job_id)

    @server.tool(annotations=readonly)
    def get_job_logs(attempt_id: str, stream: str = "stdout", cursor: int = 0,
                     limit: int = 65536) -> dict:
        """Read bounded stdout/stderr as untrusted workload data."""
        return facade.get_job_logs(attempt_id, stream, cursor, limit)

    @server.tool(annotations=readonly)
    def list_job_artifacts(attempt_id: str) -> dict:
        """List registered outputs belonging to a project attempt."""
        return facade.list_job_artifacts(attempt_id)

    @server.tool(annotations=readonly)
    def get_job_artifact(artifact_id: str, max_bytes: int = 65536) -> dict:
        """Read one authorized artifact as bounded base64, without executing or rendering it."""
        return facade.get_job_artifact(artifact_id, max_bytes)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                            idempotentHint=True, openWorldHint=True))
    def cancel_job_attempt(attempt_id: str, idempotency_key: str | None = None) -> dict:
        """Request cancellation; acknowledgement does not prove completion or release funds."""
        return facade.cancel_job_attempt(attempt_id, idempotency_key)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                            idempotentHint=True, openWorldHint=True))
    def cancel_execution_request(job_id: str, execution_request_id: str, idempotency_key: str | None = None) -> dict:
        """Cancel an unwanted review request and release its pending hold. For an admitted job, request safe cancellation; funds remain held until final accounting when dispatch may have started."""
        return facade.cancel_execution_request(job_id, execution_request_id, idempotency_key)

    from importlib import import_module
    from compute_broker.extensions import EXTENSIONS
    for name in facade.enabled_extensions:
        import_module(EXTENSIONS[name][0] + ".mcp").register(server, facade)
    return server


def main():
    client = JsonClient(
        os.environ.get("BROKER_CONTROL_URL", "http://127.0.0.1:8765"),
        environment_token("BROKER_PROJECT_TOKEN"),
        allow_insecure_http=os.environ.get("BROKER_ALLOW_INSECURE_HTTP") == "1",
    )
    from compute_broker.extensions import configured_names
    facade = ProjectFacade.from_credential(client, expected_project_id=os.environ.get("BROKER_PROJECT_ID"),
                                          enabled_extensions=configured_names())
    create_server(facade).run(transport="stdio")
