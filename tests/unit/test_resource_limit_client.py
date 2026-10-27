"""Only numeric resource diagnostics may cross the HTTP error boundary."""

import asyncio
import copy
import io
import json
from urllib.error import HTTPError

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from compute_relay.mcp.facade import ProjectFacade, create_server
from compute_relay.worker.client import ControlError, JsonClient


TOKEN = "private-project-token-do-not-expose"
PRIVATE = "/private/identity/key-file-do-not-expose"


def resource_error():
    return {"error": {"code": "RESOURCE_LIMIT", "message": PRIVATE + TOKEN, "details": {
        "violations": [
            {"resource": "cpu_cores", "requested": 3, "effective": 4, "limit": 2,
             "excess": 2, "source": "project", "unit": "cores"},
            {"resource": "memory_mib", "requested": 4096, "effective": 4096, "limit": 2048,
             "excess": 2048, "source": "cluster", "unit": "MiB"},
        ], "private_context": PRIVATE + TOKEN,
    }}}


class ErrorBody(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class ErrorOpener:
    def __init__(self, content, status=400):
        self.body = ErrorBody(content)
        self.status = status
        self.calls = []

    def open(self, request, **kwargs):
        self.calls.append(request)
        raise HTTPError(request.full_url, self.status, "private " + TOKEN, {}, self.body)


def failing_client(content, *, maximum=4 * 1024 * 1024):
    client = JsonClient("http://127.0.0.1:8768", TOKEN, max_response_bytes=maximum)
    client._opener = ErrorOpener(content)
    return client


def assert_private_redacted(error):
    text = str(error) + repr(error.details) + repr(error.args)
    assert TOKEN not in text and PRIVATE not in text


def test_http_resource_limit_preserves_only_validated_numeric_details():
    document = resource_error()
    document["error"]["details"]["violations"][0]["private_extra"] = PRIVATE + TOKEN
    client = failing_client(json.dumps(document).encode())
    with pytest.raises(ControlError) as caught:
        client.request("POST", "/api/v1/jobs", {})
    error = caught.value
    assert error.code == "CONTROL_HTTP_ERROR" and error.status == 400
    assert error.broker_code == "RESOURCE_LIMIT"
    assert error.details == {"violations": resource_error()["error"]["details"]["violations"]}
    assert "CPU cores: effective 4 cores, limit 2 cores (project; requested 3 cores; excess 2 cores)" in str(error)
    assert "Memory: effective 4096 MiB, limit 2048 MiB (cluster" in str(error)
    assert "revise the resources before retrying" in str(error)
    assert_private_redacted(error)
    assert client._opener.body.closed
    assert client._opener.calls[0].get_header("Authorization") == "Bearer " + TOKEN
    assert len(client._opener.calls) == 1


@pytest.mark.parametrize("resource,unit", [
    ("gpu_devices", "devices"), ("time_limit_seconds", "seconds"),
])
def test_remaining_allowlisted_resource_units(resource, unit):
    document = resource_error()
    document["error"]["details"]["violations"] = [{
        "resource": resource, "requested": 4, "effective": 6, "limit": 4,
        "excess": 2, "source": "cluster", "unit": unit,
    }]
    with pytest.raises(ControlError) as caught:
        failing_client(json.dumps(document).encode()).request("POST", "/api/v1/jobs", {})
    assert caught.value.broker_code == "RESOURCE_LIMIT"
    assert caught.value.details["violations"][0]["resource"] == resource


@pytest.mark.parametrize("mutation", [
    lambda error: error.update(code="AUTH_REQUIRED"),
    lambda error: error.pop("details"),
    lambda error: error.update(details=[]),
    lambda error: error["details"].update(violations=[]),
    lambda error: error["details"].update(violations="private"),
    lambda error: error["details"].update(violations=[None]),
    lambda error: error["details"].update(violations=error["details"]["violations"] * 6),
    lambda error: error["details"]["violations"].append(copy.deepcopy(error["details"]["violations"][0])),
    *[lambda error, key=key, value=value: error["details"]["violations"][0].update({key: value})
      for key, value in [
          ("resource", PRIVATE + TOKEN), ("resource", []), ("source", PRIVATE + TOKEN),
          ("source", {}), ("unit", PRIVATE + TOKEN), ("effective", 2),
          ("requested", True), ("effective", "4"), ("limit", -1), ("excess", 3),
          ("requested", 1.5), ("effective", 2**53),
      ]],
])
def test_malformed_or_unrelated_error_body_stays_generic(mutation):
    document = resource_error()
    mutation(document["error"])
    with pytest.raises(ControlError) as caught:
        failing_client(json.dumps(document).encode()).request("POST", "/api/v1/jobs", {})
    error = caught.value
    assert str(error) == "CONTROL_HTTP_ERROR"
    assert error.broker_code is None and error.details == {}
    assert_private_redacted(error)


@pytest.mark.parametrize("content", [
    b"<html>private error</html>", b"[]", b"null", b'{"error":NaN}', b'\xff',
    b'{"error":{"code":"RESOURCE_LIMIT","code":"RESOURCE_LIMIT"}}',
    b"[" * 1500,
])
def test_invalid_error_json_stays_generic(content):
    with pytest.raises(ControlError) as caught:
        failing_client(content).request("POST", "/api/v1/jobs", {})
    assert str(caught.value) == "CONTROL_HTTP_ERROR"


@pytest.mark.parametrize("maximum,expected_read", [(32, 33), (4 * 1024 * 1024, 65537)])
def test_error_body_is_bounded_before_json_parsing(maximum, expected_read):
    content = json.dumps(resource_error()).encode() + b" " * 70000
    client = failing_client(content, maximum=maximum)
    with pytest.raises(ControlError) as caught:
        client.request("GET", "/api/v1/connections")
    assert str(caught.value) == "CONTROL_HTTP_ERROR"
    assert client._opener.body.read_sizes == [expected_read]
    assert client._opener.body.closed


def test_official_mcp_tool_error_surfaces_numeric_limits_without_remote_prose():
    client = failing_client(json.dumps(resource_error()).encode())
    facade = ProjectFacade(client, "608ef015-5813-4e8f-a6ab-7ec182f406d7")
    server = create_server(facade)
    request = {"schema_version": "1.0", "name": "bounded-task", "connection_id": "608ef015-5813-4e8f-a6ab-7ec182f406d7",
        "environment": {"id": "608ef015-5813-4e8f-a6ab-7ec182f406d7", "revision_sha256": "e" * 64},
        "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
        "resources": {"nodes": 1, "cpus": 4, "gpus": 0, "memory_mib": 4096, "time_limit_seconds": 60},
        "slurm": {"partition": "cpu"}, "files": [{"path": "main.py", "sha256": "a" * 64,
            "size_bytes": 10, "executable": False}], "outputs": []}
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool("prepare_job", {"request": request}))
    message = str(caught.value)
    assert "RESOURCE_LIMIT" in message and "effective 4 cores, limit 2 cores" in message
    assert "Memory: effective 4096 MiB, limit 2048 MiB" in message
    assert TOKEN not in message and PRIVATE not in message
    assert len(client._opener.calls) == 1  # No automatic create/prepare retry after rejection.


def test_mcp_instructions_and_tools_explain_current_effective_limits():
    facade = ProjectFacade(None, "608ef015-5813-4e8f-a6ab-7ec182f406d7")
    server = create_server(facade)
    from compute_relay.mcp.guidance import guide
    workflow = " ".join(guide("workflow")["text"].split())
    placement = " ".join(guide("placement")["text"].split())
    assert "get_compute_guide" in server.instructions
    assert "Do not repeatedly submit an unchanged rejected request" in workflow
    assert "requested walltime" in placement.lower()
    assert "margin" not in server.instructions
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert "effective_limits" in tools["list_compute_targets"].description
    assert "authority" in tools["get_project_context"].description
    assert "reservations" in tools["get_project_allowance"].description
    assert "RESOURCE_LIMIT" in workflow
    assert "Prepare" in tools["prepare_job"].description


@pytest.mark.parametrize("code,guidance", [
    ("NOT_PREPARED", "inspect_environment"), ("INPUT_DENIED", "explicit upload"),
    ("SCHEDULER_OPTIONS_REQUIRED", "partition, account and QOS"),
    ("INSPECTION_ALREADY_REQUESTED", "cancel its job"),
    ("IDEMPOTENCY_CONFLICT", "original arguments"), ("ACTIVE_JOB_LIMIT", "list_project_jobs"),
    ("INVALID_REQUEST", "resource, scheduler and input fields"),
])
def test_known_error_codes_have_local_actionable_guidance_without_remote_content(code, guidance):
    document = {"error": {"code": code, "message": PRIVATE + TOKEN, "details": {"private": PRIVATE + TOKEN}}}
    with pytest.raises(ControlError) as caught:
        failing_client(json.dumps(document).encode()).request("POST", "/api/v1/jobs", {})
    error = caught.value
    assert error.code == "CONTROL_HTTP_ERROR" and error.status == 400
    assert error.broker_code == code and error.details == {}
    assert code in str(error) and "HTTP 400" in str(error) and guidance in str(error)
    assert_private_redacted(error)


@pytest.mark.parametrize("code", [PRIVATE + TOKEN, [], {}, 13, None, "RESOURCE_LIMIT\nNOT_PREPARED"])
def test_unrecognized_or_malformed_error_code_is_never_echoed(code):
    with pytest.raises(ControlError) as caught:
        failing_client(json.dumps({"error": {"code": code, "message": PRIVATE + TOKEN}}).encode()).request("GET", "/api/v1/context")
    assert str(caught.value) == "CONTROL_HTTP_ERROR" and caught.value.broker_code is None
    assert_private_redacted(caught.value)


def test_official_probe_tool_reports_safe_scheduler_recovery_guidance():
    client = failing_client(json.dumps({"error": {"code": "SCHEDULER_OPTIONS_REQUIRED", "message": PRIVATE + TOKEN}}).encode())
    server = create_server(ProjectFacade(client, "608ef015-5813-4e8f-a6ab-7ec182f406d7", enabled_extensions=["environments"]))
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool("inspect_environment", {"environment_id": "608ef015-5813-4e8f-a6ab-7ec182f406d7"}))
    assert "SCHEDULER_OPTIONS_REQUIRED" in str(caught.value) and "list_compute_targets" in str(caught.value)
    assert PRIVATE not in str(caught.value) and TOKEN not in str(caught.value)
