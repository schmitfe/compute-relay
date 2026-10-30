"""Official SDK stdio server forwarding a project's bounded API operations."""

import base64
import json
import os
import re
import sys
from urllib.parse import urlencode
from uuid import UUID, uuid4

from compute_relay.worker.client import ControlError, JsonClient, environment_token
from .implementation import adapter_metadata
from .responses import compact_allowance, compact_job


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Use a valid broker object UUID.") from None


def _preparation_key(value):
    if value is None:
        return str(uuid4())
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value):
        raise ValueError("Preparation idempotency keys must use 1–120 letters, digits, underscores, dots, colons or hyphens.")
    return value


class ProjectFacade:
    def __init__(self, client, project_id: str, enabled_extensions=()):
        self.client = client
        self.project_id = _uuid(project_id)
        from compute_relay.extensions import configured_names
        self.enabled_extensions = configured_names(",".join(enabled_extensions))

    @classmethod
    def from_credential(cls, client, *, enabled_extensions=()):
        context = client.request("GET", "/api/v1/context")
        project_id = _uuid(context["project"]["id"])
        return cls(client, project_id, enabled_extensions)

    def get_project_context(self):
        context = self.client.request("GET", "/api/v1/context")
        if _uuid(context["project"]["id"]) != self.project_id:
            raise ValueError("API key project changed; reconnect the MCP adapter")
        return {**context, "adapter": adapter_metadata()}

    def list_project_jobs(self, state="attention", cursor=None, limit=20, investigation_id=None):
        if state not in {"attention", "unfinished", "unread_results", "all"} or not 1 <= limit <= 100:
            raise ValueError("Invalid job listing options")
        query = {"state": state, "limit": limit}
        if cursor:
            query["cursor"] = cursor
        if investigation_id is not None:
            query["context"] = json.dumps({"investigations": _uuid(investigation_id)}, separators=(",", ":"))
        return self.client.request("GET", f"/api/v1/projects/{self.project_id}/jobs?" + urlencode(query))

    def acknowledge_job_results(self, attempt_id):
        return self._mutate(f"/api/v1/attempts/{_uuid(attempt_id)}/acknowledge-results", {})

    def cancel_prepared_job(self, job_id):
        return self._mutate(f"/api/v1/jobs/{_uuid(job_id)}/cancel-prepared", {})

    def get_compute_availability(self, connection_ids=None, refresh=False):
        ids = [_uuid(value) for value in connection_ids] if connection_ids is not None else None
        result = self.client.request("POST" if refresh else "GET", f"/api/v1/projects/{self.project_id}/availability",
            {"connection_ids": ids} if refresh and ids is not None else {} if refresh else None)
        if ids is not None:
            if set(ids) - {value["id"] for value in result["connections"]}:
                raise ValueError("A selected cluster is outside this key's scope")
            result["connections"] = [value for value in result["connections"] if value["id"] in ids]
        return result

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

    def get_project_allowance(self, detail: bool = False):
        result = self.client.request("GET", f"/api/v1/projects/{self.project_id}/allowance")
        return {**result, "response_mode": "detail"} if detail else compact_allowance(result)

    def upload_job_file(self, path: str, content_base64: str):
        # The facade never opens a caller-supplied local path or reads a URL.
        if len(content_base64) > 700_000:
            raise ValueError("MCP demo uploads are limited to 512 KiB")
        decoded = base64.b64decode(content_base64, validate=True)
        if len(decoded) > 512 * 1024:
            raise ValueError("MCP demo uploads are limited to 512 KiB")
        return self._mutate(f"/api/v1/projects/{self.project_id}/objects",
                            {"path": path, "content_base64": content_base64})

    def prepare_job(self, request: dict, idempotency_key: str | None = None, *, investigation_id=None, role="core"):
        request = self.project_request(request)
        key = _preparation_key(idempotency_key)
        if investigation_id is not None:
            if role not in {"core", "validation", "exploration"}:
                raise ValueError("Invalid investigation role")
            request = {"request": request, "context": {"investigations": {"id": _uuid(investigation_id), "role": role}}}
        created = self._mutate("/api/v1/jobs", request, key)
        job_id = _uuid(created["id"])
        return compact_job(self._mutate(f"/api/v1/jobs/{job_id}/prepare", {}, key + ":prepare"),
                           broker_url=getattr(self.client, "base_url", None))

    def request_execution(self, job_id: str, idempotency_key: str | None = None):
        job_id = _uuid(job_id)
        # An omitted key denotes the first execution of this immutable job.
        # Deliberate repeats remain available with an explicit new key.
        return self._mutate(f"/api/v1/jobs/{job_id}/execution-requests", {},
            idempotency_key if idempotency_key is not None else "execute:" + job_id)

    def revise_job(self, job_id: str, request: dict, idempotency_key: str | None = None):
        request = self.project_request(request)
        key = _preparation_key(idempotency_key)
        created = self._mutate(f"/api/v1/jobs/{_uuid(job_id)}/revisions", request, key)
        return compact_job(self._mutate(f"/api/v1/jobs/{_uuid(created['id'])}/prepare", {}, key + ":prepare"),
                           broker_url=getattr(self.client, "base_url", None))

    def get_job(self, job_id: str, detail: bool = False, after_revision=None, wait_seconds=0):
        query = self._wait_query(after_revision, wait_seconds)
        result = self.client.request("GET", f"/api/v1/jobs/{_uuid(job_id)}" + query)
        return {**result, "response_mode": "detail"} if detail else compact_job(result, broker_url=getattr(self.client, "base_url", None))

    @staticmethod
    def _wait_query(after_revision, wait_seconds):
        if (type(wait_seconds) is not int or not 0 <= wait_seconds <= 20
                or after_revision is not None and (not isinstance(after_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", after_revision))
                or wait_seconds and after_revision is None):
            raise ValueError("Use a snapshot revision and a wait of 0 through 20 seconds")
        return "?" + urlencode({"after_revision": after_revision, "wait_seconds": wait_seconds}) if after_revision is not None else ""

    def get_job_completion(self, job_id, cursor=0, limit=20, execution_cursor=0):
        if (any(type(value) is not int or not 0 <= value <= 2**53 - 1 for value in (cursor, execution_cursor))
                or type(limit) is not int or not 1 <= limit <= 20):
            raise ValueError("Invalid completion pagination")
        return self.client.request("GET", f"/api/v1/jobs/{_uuid(job_id)}/completion?" + urlencode({"cursor": cursor, "limit": limit, "execution_cursor": execution_cursor}))

    def list_investigations(self, state="open", cursor=0, limit=20):
        if state not in {"open", "closed", "all"} or type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid investigation listing")
        return self.client.request("GET", f"/api/v1/projects/{self.project_id}/investigations?" + urlencode({"state": state, "cursor": cursor, "limit": limit}))

    def get_investigation(self, identifier, after_revision=None, wait_seconds=0):
        return self.client.request("GET", f"/api/v1/investigations/{_uuid(identifier)}" + self._wait_query(after_revision, wait_seconds))

    def add_investigation_jobs(self, identifier, jobs, role="core", key=None):
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= 32 or role not in {"core", "validation", "exploration"}:
            raise ValueError("Attach 1 through 32 jobs with an investigation role")
        return self._mutate(f"/api/v1/investigations/{_uuid(identifier)}/jobs",
            {"jobs": [{"job_id": _uuid(job), "role": role} for job in jobs]}, key)

    def finish_investigation(self, identifier, conclusion, report, cancel_unstarted=False, key=None):
        return self._mutate(f"/api/v1/investigations/{_uuid(identifier)}/finish",
            {"conclusion": conclusion, "report": report, "cancel_unstarted": cancel_unstarted}, key)

    def get_job_logs(self, attempt_id: str, stream: str = "stdout", cursor: int = 0,
                     limit: int = 8192):
        if stream not in {"stdout", "stderr"} or not 0 <= cursor <= 2**53 - 1 or not 1 <= limit <= 65536:
            raise ValueError("invalid log request")
        query = urlencode({"stream": stream, "cursor": cursor, "limit": limit})
        return self.client.request("GET", f"/api/v1/attempts/{_uuid(attempt_id)}/logs?{query}")

    def list_job_artifacts(self, attempt_id: str):
        return self.client.request("GET", f"/api/v1/attempts/{_uuid(attempt_id)}/artifacts")

    def retry_job_outputs(self, attempt_id: str):
        return self._mutate(f"/api/v1/attempts/{_uuid(attempt_id)}/collect", {})

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
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.types import ToolAnnotations
    from pydantic import ValidationError as ArgumentValidationError
    from .requests import JobRequest

    class BrokerMCP(FastMCP):
        async def call_tool(self, name, arguments):
            try:
                return await super().call_tool(name, arguments)
            except (ToolError, ArgumentValidationError) as error:
                cause = error if isinstance(error, ArgumentValidationError) else error.__cause__
                if isinstance(cause, ArgumentValidationError):
                    # FastMCP's outer argument model is separate from JobRequest.
                    # Its default exception text can echo the entire rejected
                    # call. Select only declared argument names and fixed labels;
                    # suppress the original traceback for SDK stderr handlers.
                    definition = next((tool for tool in await self.list_tools() if tool.name == name), None)
                    allowed = set(definition.inputSchema.get("properties", {})) if definition else set()
                    fields = []
                    labels = {"missing": "required", "string_type": "must be a string",
                        "int_type": "must be an integer", "int_parsing": "must be an integer",
                        "int_from_float": "must be an integer", "bool_type": "must be a boolean",
                        "bool_parsing": "must be a boolean", "dict_type": "must be an object",
                        "list_type": "must be an array"}
                    for item in cause.errors(include_input=False, include_context=False, include_url=False):
                        location = item.get("loc", ())
                        field = location[0] if location and location[0] in allowed else "argument"
                        text = field + ": " + labels.get(item.get("type"), "invalid type or value")
                        if text not in fields:
                            fields.append(text)
                    raise ToolError("Invalid tool arguments: " + "; ".join(fields[:8]) + ".") from None
                if not any(tool.name == name for tool in await self.list_tools()):
                    raise ToolError("Unknown broker tool.") from None
                raise

    server = BrokerMCP("Compute Relay", instructions=(
        "Loaded local stdio adapter: " + json.dumps(adapter_metadata(), separators=(",", ":")) + ". "
        "This identifies the adapter, not the broker service. "
        "Call get_project_context once for project authority and recovery. Shared guidance is in "
        "compute-relay://guide/workflow, /placement, /completion and /job_request; tool-only clients "
        "can use get_compute_guide(topic). Read relevant guidance once when planning, choosing "
        "placement or finishing work. Job responses default to compact; detail=True is explicit. "
        "Project policy and human review remain authoritative. Execution, retained outputs, local "
        "hash verification, accounting, acknowledgement and scientific validity are separate states."
    ))
    from .guidance import guide, register_resources
    from typing import Literal
    register_resources(server)
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                               idempotentHint=True, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                            idempotentHint=False, openWorldHint=True)

    @server.tool(annotations=readonly)
    def get_compute_guide(topic: Literal["workflow", "placement", "completion", "job_request"] = "workflow") -> dict:
        """Read shared guidance on demand; job_request includes typed resources and a small valid example."""
        return guide(topic)

    @server.tool(annotations=readonly)
    def get_job_completion(job_id: str, cursor: int = 0, limit: int = 20, execution_cursor: int = 0) -> dict:
        """Read a paginated run manifest: provenance, submission, outputs, accounting and delivery evidence. Reading changes no execution state."""
        return facade.get_job_completion(job_id, cursor, limit, execution_cursor)

    @server.tool(annotations=readonly)
    def get_project_context() -> dict:
        """Read current project authority, targets, environments, allowance and recovery inbox; no project ID is needed."""
        return facade.get_project_context()

    @server.tool(annotations=readonly)
    def get_broker_capabilities() -> dict:
        """Optional: inspect enabled extensions when an extension tool is needed. Ordinary job planning uses get_project_context."""
        return facade.get_broker_capabilities()

    @server.tool(annotations=readonly)
    def list_project_jobs(state: str = "attention", cursor: str | None = None, limit: int = 20, investigation_id: str | None = None) -> dict:
        """List recoverable project work or filter by investigation. Follow next_cursor; reading never executes or cancels work."""
        return facade.list_project_jobs(state, cursor, limit, investigation_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def acknowledge_job_results(attempt_id: str) -> dict:
        """Mark a finished attempt's results as acknowledged for this key after reviewing or recording them. This does not assert scientific validity, cancel work or alter accounting."""
        return facade.acknowledge_job_results(attempt_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
    def cancel_prepared_job(job_id: str) -> dict:
        """Dismiss an abandoned draft/prepared job with no execution requests, preserving its package/history. Requires jobs:cancel. Jobs with execution requests use their existing cancellation tools."""
        return facade.cancel_prepared_job(job_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    def get_compute_availability(connection_ids: list[str] | None = None, refresh: bool = False) -> dict:
        """Read scheduler capacity and queue pressure. refresh=True requests a bounded observation; placement interpretation is in the placement guide."""
        return facade.get_compute_availability(connection_ids, refresh)

    @server.tool(annotations=readonly)
    def list_compute_targets() -> dict:
        """Optional focused refresh of permitted clusters and effective_limits. get_project_context already contains these targets. Does not log in to a cluster."""
        return facade.list_compute_targets()

    @server.tool(annotations=readonly)
    def get_project_allowance(detail: bool = False) -> dict:
        """Refresh balances, reservations, debt, expiry and admission counts. detail=True includes full allowance metadata."""
        return facade.get_project_allowance(detail=detail)

    @server.tool(annotations=write)
    def upload_job_file(path: str, content_base64: str) -> dict:
        """Upload one base64-encoded source file (512 KiB maximum); path is a logical name."""
        return facade.upload_job_file(path, content_base64)

    @server.tool(annotations=write)
    def prepare_job(request: JobRequest, idempotency_key: str | None = None, investigation_id: str | None = None, role: Literal["core", "validation", "exploration"] = "core") -> dict:
        """Prepare an immutable package without executing it. Use current target/environment and staged file descriptors; optional investigation_id groups the stage."""
        return facade.prepare_job(request.root, idempotency_key, investigation_id=investigation_id, role=role)

    @server.tool(annotations=write)
    def revise_job(job_id: str, request: JobRequest, idempotency_key: str | None = None) -> dict:
        """Prepare a linked revision after review feedback. Omit project_id; changes need a new execution request."""
        return facade.revise_job(job_id, request.root, idempotency_key)

    @server.tool(annotations=write)
    def request_execution(job_id: str, idempotency_key: str | None = None) -> dict:
        """Request execution and reserve budget; returns approval reasons and state. Retries reuse the default job key; a new explicit key means a deliberate repeat."""
        return facade.request_execution(job_id, idempotency_key)

    @server.tool(annotations=readonly)
    def get_job(job_id: str, detail: bool = False, after_revision: str | None = None, wait_seconds: int = 0) -> dict:
        """Read status, execution progress and completion blockers. detail=True includes package/history. after_revision waits up to 20 seconds; unchanged timeouts return changed=false."""
        return facade.get_job(job_id, detail=detail, after_revision=after_revision, wait_seconds=wait_seconds)

    @server.tool(annotations=readonly)
    def get_job_logs(attempt_id: str, stream: str = "stdout", cursor: int = 0,
                     limit: int = 8192) -> dict:
        """Read a bounded live-log window. Reuse next_cursor; eof drains the snapshot, finished marks terminal execution, and truncated/reset flag gaps."""
        return facade.get_job_logs(attempt_id, stream, cursor, limit)

    @server.tool(annotations=readonly)
    def list_job_artifacts(attempt_id: str) -> dict:
        """List registered outputs belonging to a project attempt."""
        return facade.list_job_artifacts(attempt_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=True, openWorldHint=True))
    def retry_job_outputs(attempt_id: str) -> dict:
        """Resume collecting declared outputs of a finished attempt without resubmitting compute or changing accounting. Requires jobs:write. Download large files with compute-relay files fetch."""
        return facade.retry_job_outputs(attempt_id)

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
    from compute_relay.extensions import EXTENSIONS
    for name in facade.enabled_extensions:
        import_module(EXTENSIONS[name][0] + ".mcp").register(server, facade)
    return server


def _startup_extensions(client):
    """Discover enabled fixed-registry modules; explicit local settings narrow.

    Capabilities come from the same authenticated broker as the project context.
    They select only reviewed bundled registration code, never an import path or
    additional authority. A failed/malformed query must not hide server features.
    """
    from compute_relay.extensions import EXTENSIONS, configured_names
    capabilities = client.request("GET", "/api/v1/capabilities")
    if not isinstance(capabilities, dict) or capabilities.get("api_version") != "1.0":
        raise ValueError("Invalid broker capabilities response")
    names = capabilities.get("enabled_extensions")
    if (not isinstance(names, list) or len(names) > len(EXTENSIONS)
            or any(not isinstance(name, str) or name not in EXTENSIONS for name in names)):
        raise ValueError("Invalid enabled module list")
    enabled = configured_names(",".join(names))
    if "BROKER_EXTENSIONS" in os.environ:
        selected = configured_names()
        return tuple(name for name in enabled if name in selected)
    return enabled


def main():
    try:
        client = JsonClient(
            os.environ.get("BROKER_CONTROL_URL", "http://127.0.0.1:8768"),
            environment_token("BROKER_PROJECT_TOKEN"),
            allow_insecure_http=os.environ.get("BROKER_ALLOW_INSECURE_HTTP") == "1",
        )
        facade = ProjectFacade.from_credential(client)
        facade.enabled_extensions = _startup_extensions(client)
        server = create_server(facade)
    except Exception as error:
        if isinstance(error, ControlError) and error.status in {401, 403}:
            message = "The project API key was rejected or lacks jobs:read access. Check its current permissions."
        elif isinstance(error, ValueError):
            message = "The broker context, capabilities or local module selection is invalid. Check the current broker and configuration."
        else:
            message = "The authenticated broker query or module setup failed. Check the broker URL, service and private project key."
        # Startup never writes protocol chatter, raw responses, paths or secret
        # exception contents to stdout/stderr. Runtime remains owned by the SDK.
        sys.stderr.write("Compute Relay MCP startup failed: " + message + "\n")
        raise SystemExit(1) from None
    server.run(transport="stdio")
