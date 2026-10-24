"""MCP defaults keep polling/context small and publish complete job input types."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from uuid import uuid4

from jsonschema import Draft202012Validator
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from compute_relay.mcp.facade import ProjectFacade, create_server
from compute_relay.mcp import implementation
from compute_relay.mcp.requests import JobRequest, client_job_schema
from compute_relay.mcp.responses import compact_allowance, compact_job
from test_mcp import ClientSpy


@pytest.fixture
def job_request():
    return {"schema_version": "1.0", "connection_id": str(uuid4()), "name": "science-check",
        "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
        "environment": {"id": str(uuid4()), "revision_sha256": "e" * 64},
        "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
        "slurm": {"partition": "cpu"},
        "files": [{"path": "main.py", "sha256": "a" * 64, "size_bytes": 10, "executable": False}],
        "outputs": ["result.json"]}


@pytest.fixture
def job_payload(job_request):
    approval = {"requires_human_approval": True, "approval_reasons": [{"code": "PROJECT_REVIEW_REQUIRED",
        "source": "project", "message": "The project is set to review every execution."}]}
    return {"id": str(uuid4()), "name": "science-check", "state": "awaiting_approval", "manifest_sha256": "c" * 64,
        "request": deepcopy(job_request), "manifest": {"request": deepcopy(job_request),
            "reservation": {"cpu_core_seconds": 60, "gpu_device_seconds": 0}},
        "resource_validation": {"valid": True, "violations": [], "project_envelope_human_override_allowed": False,
            "effective_limits": {"per_job_limits": {"nodes": 1, "cpu_cores": 8, "gpu_devices": 0,
                "memory_mib": 1024, "time_limit_seconds": 600}, "notes": ["Long repeated planning notes"]}},
        "approval": deepcopy(approval), "execution_requests": [{"id": str(uuid4()), "state": "awaiting_approval",
            "comment": None, "approval": deepcopy(approval), "approval_current": deepcopy(approval),
            "budget_reservation": {"state": "held", "cpu_core_seconds": 60, "gpu_device_seconds": 0}}], "attempts": []}


def test_summary_keeps_latest_review_reservation_and_next_action_without_duplicate_context(job_payload):
    original = deepcopy(job_payload)
    summary = compact_job(job_payload, broker_url="https://broker.example")
    assert summary["response_mode"] == "compact"
    assert summary["id"] == job_payload["id"]
    assert summary["latest_execution"]["budget_reservation"]["state"] == "held"
    assert summary["reservation"] == {"cpu_core_seconds": 60, "gpu_device_seconds": 0}
    assert summary["next_action"] == {"action": "await_human_review"}
    assert summary["review_url"] == f"https://broker.example/review/{job_payload['id']}/"
    assert summary["resource_validation"]["effective_limits"]["cpu_cores"] == 8
    assert "approval_current" not in summary
    assert not {"request", "manifest", "files", "attempts", "execution_requests"} & summary.keys()
    summary["approval"]["approval_reasons"][0]["message"] = "Changed locally"
    assert job_payload == original


def test_summary_size_stays_bounded_with_long_history_and_large_prepared_metadata(job_payload):
    job_payload["request"]["command"]["arguments"] = ["a" * 4096 for _ in range(64)]
    job_payload["manifest"]["request"] = deepcopy(job_payload["request"])
    latest = deepcopy(job_payload["execution_requests"][-1])
    job_payload["execution_requests"] = [{**deepcopy(latest), "id": str(uuid4()), "comment": "old-feedback" * 30}
        for _ in range(1000)] + [latest]
    job_payload["files"] = [{"path": "main.py", "text": "hidden-source" * 10000}]
    raw_size = len(json.dumps(job_payload).encode())
    summary = compact_job(job_payload)
    compact_size = len(json.dumps(summary).encode())
    assert compact_size < 4096 and compact_size * 100 < raw_size
    assert "hidden-source" not in json.dumps(summary)
    assert "old-feedback" not in json.dumps(summary)


def test_failed_attempt_summary_retains_safe_actual_accounting_and_scheduler_outcome(job_payload):
    job_payload["execution_requests"][-1]["state"] = "authorized"
    job_payload["attempts"] = [{"id": str(uuid4()), "execution_request_id": job_payload["execution_requests"][-1]["id"], "state": "failed", "accounting_state": "settled",
        "error_code": "OBSERVED_OVERAGE", "native_state": "FAILED", "exit_code": "1:0",
        "native_identity": {"job_id": "42", "cluster": "science", "work_dir": "/private/job/path"},
        "stdout": "large science output", "accounting": {"final": True, "cpu_core_seconds": 90, "gpu_device_seconds": 0,
            "elapsed_seconds": 90, "requested": {"cpu_cores": 1, "gpu_devices": 0, "time_limit_seconds": 60},
            "allocated": {"cpu_cores": 1, "gpu_devices": 0}, "reservation": {"cpu_core_seconds": 60, "gpu_device_seconds": 0},
            "reservation_difference": {"cpu_core_seconds": 30, "gpu_device_seconds": 0}, "raw_remote_log": "unneeded"}}]
    summary = compact_job(job_payload)
    assert summary["latest_attempt"]["accounting"]["cpu_core_seconds"] == 90
    assert summary["latest_attempt"]["exit_code"] == "1:0"
    assert summary["latest_attempt"]["native_identity"] == {"job_id": "42", "cluster": "science"}
    assert summary["next_action"]["tool"] == "list_job_artifacts"
    assert summary["result_availability"]["artifacts"] == "not_checked"
    assert "large science output" not in json.dumps(summary) and "unneeded" not in json.dumps(summary)


@pytest.mark.parametrize("execution_state,next_action", [("rejected", "revise_request"), ("cancelled", "none"),
    ("expired", "none"), ("awaiting_approval", "await_human_review")])
def test_newest_review_state_is_not_hidden_by_old_successful_attempt(job_payload, execution_state, next_action):
    job_payload["execution_requests"][-1].update(state=execution_state, comment="Please reduce memory.")
    job_payload["attempts"] = [{"id": str(uuid4()), "execution_request_id": str(uuid4()), "state": "succeeded", "accounting_state": "settled"}]
    summary = compact_job(job_payload)
    assert summary["latest_execution"]["comment"] == "Please reduce memory."
    assert summary["next_action"]["action"] == next_action
    assert "latest_attempt" not in summary and summary["previous_attempt_count"] == 1
    assert summary["result_availability"]["artifacts"] == "not_started"


def test_attempt_from_an_older_execution_is_not_mistaken_for_current_results(job_payload):
    job_payload["execution_requests"][-1]["state"] = "authorized"
    old = {"id": str(uuid4()), "execution_request_id": str(uuid4()), "state": "succeeded", "accounting_state": "settled"}
    current = {"id": str(uuid4()), "execution_request_id": job_payload["execution_requests"][-1]["id"],
        "state": "queued", "accounting_state": "reserved"}
    job_payload["attempts"] = [old, current]
    summary = compact_job(job_payload)
    assert summary["latest_attempt"]["id"] == current["id"]
    assert summary["previous_attempt_count"] == 1
    assert summary["next_action"]["action"] == "wait"


@pytest.mark.parametrize("association", [None, "", 1])
def test_attempt_response_requires_current_execution_request_association(job_payload, association):
    job_payload["attempts"] = [{"id": str(uuid4()), "execution_request_id": association,
        "state": "succeeded", "accounting_state": "settled"}]
    with pytest.raises(ValueError, match="must identify their execution request"):
        compact_job(job_payload)


def test_get_job_full_metadata_is_explicit_and_uses_no_extra_http_calls(job_payload):
    class Client(ClientSpy):
        base_url = "https://broker.example"
        def request(self, *args, **kwargs):
            super().request(*args, **kwargs)
            return deepcopy(job_payload)
    client = Client();facade = ProjectFacade(client, client.project_id)
    summary = facade.get_job(job_payload["id"])
    detail = facade.get_job(job_payload["id"], detail=True)
    assert summary["response_mode"] == "compact"
    assert "manifest" not in summary and detail == {**job_payload, "response_mode": "detail"}
    assert len(client.calls) == 2 and all(call[0] == "GET" for call in client.calls)


def test_adapter_metadata_identifies_the_loaded_client_and_does_not_reread_sources(monkeypatch):
    client = ClientSpy()
    original = {"schema_version": "1.0", "project": {"id": client.project_id},
        "adapter": {"implementation": "synthetic-remote-spoof"}}
    client.request = lambda *args, **kwargs: original
    expected = implementation.adapter_metadata()
    assert expected["implementation"] == "compute-relay-stdio-mcp"
    assert expected["schema_version"] == "1.0"
    assert len(expected["source_sha256"]) == 64
    assert expected["default_job_response_mode"] == "compact"
    assert expected["default_allowance_response_mode"] == "budget"

    def forbidden_read(path):
        raise AssertionError("A running adapter must keep its import-time fingerprint.")
    monkeypatch.setattr(implementation.Path, "read_bytes", forbidden_read)
    facade = ProjectFacade(client, client.project_id)
    context = facade.get_project_context()
    assert context["adapter"] == expected
    assert original["adapter"]["implementation"] == "synthetic-remote-spoof"
    context["adapter"]["implementation"] = "locally changed"
    assert facade.get_project_context()["adapter"] == expected


def test_source_fingerprint_explicitly_reports_unavailable_packaged_sources(monkeypatch):
    original = implementation.adapter_metadata()
    def unavailable(path):
        raise OSError("Synthetic source archive does not expose ordinary files.")
    monkeypatch.setattr(implementation.Path, "read_bytes", unavailable)
    assert implementation._source_fingerprint() is None
    assert implementation.adapter_metadata() == original


@pytest.fixture
def allowance_payload():
    return {"project_id": str(uuid4()), "mode": "approval_required", "paused": False,
        "policy_revision": "synthetic-revision", "admission_expires_at": "2099-01-01T00:00:00Z",
        "per_job_limits": {"cpu_cores": 8, "gpu_devices": 0, "memory_mib": 4096, "time_limit_seconds": 3600},
        "limit": {"cpu_core_seconds": 1000, "gpu_device_seconds": 0},
        "spent": {"cpu_core_seconds": 100, "gpu_device_seconds": 0},
        "reserved": {"cpu_core_seconds": 120, "gpu_device_seconds": 0},
        "reserved_awaiting_approval": {"cpu_core_seconds": 60, "gpu_device_seconds": 0},
        "available": {"cpu_core_seconds": 780, "gpu_device_seconds": 0},
        "debt": {"cpu_core_seconds": 0, "gpu_device_seconds": 0},
        "jobs": {"limit": 10, "admitted": 2, "remaining": 8, "active": 1,
            "active_limit": 2, "awaiting_approval": 1},
        "compute": {"connections": [{"id": str(uuid4()), "catalog": "repeated-discovery" * 1000}]}}


def test_allowance_summary_keeps_budget_and_submission_limits_without_repeated_compute(allowance_payload):
    original = deepcopy(allowance_payload)
    summary = compact_allowance(allowance_payload)
    assert summary["response_mode"] == "budget"
    assert summary["jobs"] == allowance_payload["jobs"]
    assert summary["reserved_awaiting_approval"]["cpu_core_seconds"] == 60
    assert summary["available"]["cpu_core_seconds"] == 780
    assert summary["admission_expires_at"] == allowance_payload["admission_expires_at"]
    assert "compute" not in summary and "per_job_limits" not in summary
    assert len(json.dumps(summary)) * 10 < len(json.dumps(allowance_payload))
    summary["available"]["cpu_core_seconds"] = 0
    assert allowance_payload == original


def test_allowance_detail_preserves_full_metadata_without_mutating_rest_payload(allowance_payload):
    client = ClientSpy()
    client.request = lambda *args, **kwargs: allowance_payload
    facade = ProjectFacade(client, client.project_id)
    assert facade.get_project_allowance()["response_mode"] == "budget"
    assert facade.get_project_allowance(detail=True) == {**allowance_payload, "response_mode": "detail"}
    assert "response_mode" not in allowance_payload


def test_client_schema_exposes_nested_job_fields_without_remote_references(job_request):
    schema = client_job_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(job_request)
    assert "project_id" not in schema["required"]
    assert schema["properties"]["resources"]["properties"]["time_limit_seconds"]["multipleOf"] == 60
    assert "slurm" in schema["properties"] and "files" in schema["required"]
    assert "$ref" not in json.dumps(schema)
    assert JobRequest.model_validate(job_request).root == job_request


@pytest.mark.parametrize("mutation", ["missing_resources", "unknown_field", "boolean_cpus", "seconds_not_minutes",
    "missing_gpu_type", "zero_gpu_type", "invalid_digest", "missing_script", "duplicate_path", "output_overwrites_input"])
def test_typed_sdk_requests_validate_canonical_contract_before_http(job_request, mutation):
    request = deepcopy(job_request)
    if mutation == "missing_resources": del request["resources"]
    elif mutation == "unknown_field": request["shell_command"] = "forbidden"
    elif mutation == "boolean_cpus": request["resources"]["cpus"] = True
    elif mutation == "seconds_not_minutes": request["resources"]["time_limit_seconds"] = 61
    elif mutation == "missing_gpu_type": request["resources"]["gpus"] = 1
    elif mutation == "zero_gpu_type": request["resources"]["gpu_type"] = "a100"
    elif mutation == "invalid_digest": request["environment"]["revision_sha256"] = "invalid"
    elif mutation == "missing_script": request["command"]["script"] = "not-uploaded.py"
    elif mutation == "duplicate_path": request["files"].append({**request["files"][0], "sha256": "b" * 64})
    else: request["outputs"] = ["main.py"]
    client = ClientSpy();server = create_server(ProjectFacade(client, client.project_id))
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("prepare_job", {"request": request}))
    assert client.calls == []


def test_sdk_tool_schema_and_normal_json_calls_support_codex_and_claude(job_request):
    client = ClientSpy();server = create_server(ProjectFacade(client, client.project_id))
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schema = tools["prepare_job"].inputSchema
    assert "resources" in json.dumps(schema) and "partition" in json.dumps(schema)
    assert tools["get_job"].inputSchema["properties"]["detail"]["default"] is False
    assert tools["get_project_allowance"].inputSchema["properties"]["detail"]["default"] is False
    assert "get_project_context once" in server.instructions
    assert implementation.adapter_metadata()["source_sha256"] in server.instructions
    asyncio.run(server.call_tool("prepare_job", {"request": deepcopy(job_request), "idempotency_key": "plain-json"}))
    assert client.calls[0][2] == {**job_request, "project_id": client.project_id}
    client.calls.clear()
    # The official SDK also accepts clients that encode object parameters as
    # JSON strings, without introducing a separate Claude server implementation.
    asyncio.run(server.call_tool("prepare_job", {"request": json.dumps(job_request), "idempotency_key": "encoded-json"}))
    assert client.calls[0][2] == {**job_request, "project_id": client.project_id}


@pytest.mark.parametrize("bad_field", ["name", "script", "argument", "extra", "container"])
def test_rejected_request_values_are_not_echoed_in_sdk_error_messages(job_request, bad_field):
    secret = "never-echo-submitted-private-source"
    request = deepcopy(job_request)
    if bad_field == "name": request["name"] = "!" + secret
    elif bad_field == "script": request["command"]["script"] = "/" + secret
    elif bad_field == "argument": request["command"]["arguments"] = [secret + "\0"]
    elif bad_field == "extra": request["unknown_" + secret] = secret
    else: request = secret
    client = ClientSpy();server = create_server(ProjectFacade(client, client.project_id))
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool("prepare_job", {"request": request}))
    assert secret not in str(caught.value)
    assert "input_value" not in str(caught.value)
    assert client.calls == []


@pytest.mark.parametrize("field,value", [
    (("resources", "cpus"), 1.0), (("files", 0, "size_bytes"), 10.0),
    (("resources", "time_limit_seconds"), 60.0), (("resources", "memory_mib"), float("nan")),
    (("resources", "cpus"), float("inf")),
])
def test_integer_valued_and_nonfinite_floats_are_rejected_before_http(job_request, field, value):
    request = deepcopy(job_request)
    parent = request
    for component in field[:-1]:
        parent = parent[component]
    parent[field[-1]] = value
    client = ClientSpy()
    server = create_server(ProjectFacade(client, client.project_id))
    with pytest.raises(ToolError, match="JSON numbers must be integers"):
        asyncio.run(server.call_tool("prepare_job", {"request": request}))
    assert not client.calls


@pytest.mark.parametrize("tool,arguments", [
    ("prepare_job", {"requests": {"source": "XSAFE"}}),
    ("prepare_job", {"idempotency_key": {"private": "XSAFE"}}),
    ("get_job", {"job_id": {"private": "XSAFE"}}),
    ("get_job", {"job_id": "XSAFE" * 6 + "ZZ"}),
    ("get_job", {"job_id": "608ef015-5813-4e8f-a6ab-7ec182f406d7", "detail": {"private": "XSAFE"}}),
    ("XSAFE-unknown-tool", {}),
])
def test_outer_sdk_errors_and_invalid_uuid_strings_do_not_echo_rejected_values(tool, arguments, caplog):
    client = ClientSpy()
    server = create_server(ProjectFacade(client, client.project_id))
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool(tool, deepcopy(arguments)))
    assert "XSAFE" not in str(caught.value) and "input_value" not in str(caught.value)
    assert "XSAFE" not in caplog.text
    assert not client.calls


def test_wrong_typed_outer_fields_are_sanitized_with_a_valid_job_document(job_request):
    client = ClientSpy()
    server = create_server(ProjectFacade(client, client.project_id))
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool("prepare_job", {"request": job_request, "idempotency_key": {"secret": "XSAFE"}}))
    assert "idempotency_key: must be a string" in str(caught.value)
    assert "XSAFE" not in str(caught.value) and not client.calls


@pytest.mark.parametrize("operation", ["prepare_job", "revise_job"])
@pytest.mark.parametrize("key", ["", "XSAFE" * 25, "XSAFE key", "XSAFE\nkey", "XSAFE/unsafe"])
def test_invalid_preparation_idempotency_keys_cannot_create_an_orphan_revision(job_request, operation, key):
    client = ClientSpy()
    server = create_server(ProjectFacade(client, client.project_id))
    arguments = {"request": deepcopy(job_request), "idempotency_key": key}
    if operation == "revise_job":
        arguments["job_id"] = client.job_id
    with pytest.raises(ToolError, match="1–120") as caught:
        asyncio.run(server.call_tool(operation, arguments))
    assert "XSAFE" not in str(caught.value) and not client.calls


@pytest.mark.parametrize("operation", ["prepare_job", "revise_job"])
def test_longest_preparation_key_keeps_both_operations_within_server_limit(job_request, operation):
    client = ClientSpy()
    server = create_server(ProjectFacade(client, client.project_id))
    key = "a" * 120
    arguments = {"request": deepcopy(job_request), "idempotency_key": key}
    if operation == "revise_job":
        arguments["job_id"] = client.job_id
    asyncio.run(server.call_tool(operation, arguments))
    assert len(client.calls) == 2
    assert client.calls[0][3]["idempotency_key"] == key
    assert client.calls[1][3]["idempotency_key"] == key + ":prepare"
    assert len(client.calls[1][3]["idempotency_key"]) == 128


def test_stdio_protocol_and_sdk_stderr_do_not_echo_invalid_argument_values(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    source = str(Path(__file__).resolve().parents[2] / "src")
    program = (
        "from compute_relay.mcp.facade import ProjectFacade,create_server;"
        "create_server(ProjectFacade(None,'608ef015-5813-4e8f-a6ab-7ec182f406d7')).run(transport='stdio')"
    )
    log = tmp_path / "sdk-stderr.log"
    async def scenario():
        parameters = StdioServerParameters(command=sys.executable, args=["-c", program], env={"PYTHONPATH": source})
        with log.open("w") as stderr:
            async with stdio_client(parameters, errlog=stderr) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    for tool, arguments in (("prepare_job", {"requests": {"source": "XSAFE"}}),
                        ("get_job", {"job_id": {"source": "XSAFE"}})):
                        result = await client.call_tool(tool, arguments)
                        assert result.isError
                        text = "".join(item.text for item in result.content if item.type == "text")
                        assert "Invalid tool arguments" in text
                        assert "XSAFE" not in text and "input_value" not in text
    asyncio.run(scenario())
    assert "XSAFE" not in log.read_text()


def test_real_stdio_defaults_are_compact_with_explicit_adapter_and_response_modes(job_payload, allowance_payload):
    """Exercise client defaults against this adapter without credentials or a broker."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    project_id = allowance_payload["project_id"]
    job_payload["files"] = [{"path": "main.py", "text": "synthetic-source" * 1200}]
    context = {"schema_version": "1.0", "project": {"id": project_id},
        "allowance": deepcopy(allowance_payload), "compute": deepcopy(allowance_payload["compute"])}
    # Passing fixed synthetic fixtures directly avoids any real token, service,
    # project filesystem, or institutional cluster access in this protocol test.
    program = "\n".join((
        "from compute_relay.mcp.facade import ProjectFacade, create_server",
        f"job = {job_payload!r}", f"allowance = {allowance_payload!r}", f"context = {context!r}",
        "class Client:",
        "    def request(self, method, path, *args, **kwargs):",
        "        if path == '/api/v1/context': return context",
        "        if path.endswith('/allowance'): return allowance",
        "        return job",
        f"create_server(ProjectFacade(Client(), {project_id!r})).run(transport='stdio')",
    ))
    async def scenario():
        parameters = StdioServerParameters(command=sys.executable, args=["-c", program],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")})
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                expected = implementation.adapter_metadata()
                assert expected["source_sha256"] in initialized.instructions
                assert "not the broker service" in initialized.instructions
                async def tool(name, **arguments):
                    response = await client.call_tool(name, arguments)
                    assert not response.isError, response
                    return json.loads("".join(item.text for item in response.content if item.type == "text"))
                discovered = await tool("get_project_context")
                assert discovered["adapter"] == expected
                assert discovered["compute"] == context["compute"]
                assert discovered["allowance"] == context["allowance"]
                summary = await tool("get_job", job_id=job_payload["id"])
                explicit_summary = await tool("get_job", job_id=job_payload["id"], detail=False)
                detail = await tool("get_job", job_id=job_payload["id"], detail=True)
                assert summary == explicit_summary
                assert summary["response_mode"] == "compact" and detail["response_mode"] == "detail"
                assert not {"request", "manifest", "files", "attempts", "execution_requests"} & summary.keys()
                assert detail["files"] == job_payload["files"]
                assert len(json.dumps(summary)) < 4096 and len(json.dumps(detail)) > 10774
                budget = await tool("get_project_allowance")
                budget_detail = await tool("get_project_allowance", detail=True)
                assert budget["response_mode"] == "budget" and "compute" not in budget
                assert budget_detail["response_mode"] == "detail" and budget_detail["compute"] == allowance_payload["compute"]
                assert budget["jobs"] == allowance_payload["jobs"]
    asyncio.run(scenario())
