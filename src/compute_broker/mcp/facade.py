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
        if request.get("project_id") != self.project_id:
            raise ValueError("job must belong to the configured project")
        key = idempotency_key or str(uuid4())
        created = self._mutate("/api/v1/jobs", request, key)
        job_id = _uuid(created["id"])
        return self._mutate(f"/api/v1/jobs/{job_id}/prepare", {}, key + ":prepare")

    def request_execution(self, job_id: str, idempotency_key: str | None = None):
        return self._mutate(f"/api/v1/jobs/{_uuid(job_id)}/execution-requests", {}, idempotency_key)

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


def create_server(facade: ProjectFacade):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("Compute Broker")
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                               idempotentHint=True, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                            idempotentHint=False, openWorldHint=True)

    @server.tool(annotations=readonly)
    def get_broker_capabilities() -> dict:
        """Read server-enabled extensions and this key's scopes before planning work."""
        return facade.get_broker_capabilities()

    @server.tool(annotations=readonly)
    def list_compute_targets() -> dict:
        """Read assigned clusters and saved capabilities; does not log in to a cluster."""
        return facade.list_compute_targets()

    @server.tool(annotations=readonly)
    def get_project_allowance() -> dict:
        """Read shared project funds, outstanding reservations, and uncertainty."""
        return facade.get_project_allowance()

    @server.tool(annotations=write)
    def upload_job_file(path: str, content_base64: str) -> dict:
        """Upload one base64-encoded source file (512 KiB maximum); path is a logical name."""
        return facade.upload_job_file(path, content_base64)

    @server.tool(annotations=write)
    def prepare_job(request: dict, idempotency_key: str | None = None) -> dict:
        """Prepare a job for review. Set request.name to a descriptive 1–80 character job name (letters, digits, _, ., -). Preparation never executes code."""
        return facade.prepare_job(request, idempotency_key)

    @server.tool(annotations=write)
    def request_execution(job_id: str, idempotency_key: str | None = None) -> dict:
        """Request approval or policy-authorized execution; returns a durable state promptly."""
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
    facade = ProjectFacade(client, os.environ["BROKER_PROJECT_ID"], configured_names())
    create_server(facade).run(transport="stdio")
