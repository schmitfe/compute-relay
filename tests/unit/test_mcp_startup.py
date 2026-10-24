"""Normal stdio startup discovers enabled modules without exposing credentials."""

import asyncio
from copy import deepcopy
import tomllib
from uuid import uuid4

import pytest
from mcp.server.fastmcp import FastMCP

from compute_relay.mcp import facade, setup
from compute_relay.worker.client import ControlError


class StartupClient:
    def __init__(self, capabilities, failure=None):
        self.project_id = str(uuid4())
        self.capabilities = capabilities
        self.failure = failure
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        if self.failure is not None:
            raise self.failure
        if path == "/api/v1/context":
            return {"schema_version": "1.0", "project": {"id": self.project_id}}
        assert path == "/api/v1/capabilities"
        return deepcopy(self.capabilities)


def startup(monkeypatch, names=("storage", "environments", "workflows"), *, document=None, failure=None):
    monkeypatch.delenv("BROKER_EXTENSIONS", raising=False)
    client = StartupClient({"api_version": "1.0", "enabled_extensions": list(names)} if document is None else document, failure)
    monkeypatch.setattr(facade, "JsonClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(facade, "environment_token", lambda _name: "cbp_private_synthetic_must_not_print")
    servers = []
    def run(server, *, transport):
        assert transport == "stdio"
        servers.append(server)
    monkeypatch.setattr(FastMCP, "run", run)
    return client, servers


def tool_names(server):
    return {tool.name for tool in asyncio.run(server.list_tools())}


def test_normal_init_mcp_starts_all_enabled_bundled_modules_without_local_extra_configuration(tmp_path, monkeypatch, capsys):
    setup.initialize(tmp_path, api_key="cbp_private_synthetic_project_key_123456789", client="codex")
    generated = tomllib.loads((tmp_path / setup.CONFIG_PATH).read_text())["mcp_servers"]["compute_relay"]
    assert "BROKER_EXTENSIONS" not in generated["env"]
    client, servers = startup(monkeypatch)
    for name, value in generated["env"].items(): monkeypatch.setenv(name, value)
    facade.main()
    names = tool_names(servers[0])
    assert {"get_project_context", "get_project_storage", "create_environment_build", "prepare_workflow"} <= names
    assert client.calls == [("GET", "/api/v1/context"), ("GET", "/api/v1/capabilities")]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("enabled,required,omitted", [
    (["storage"], {"get_project_storage"}, {"create_environment_build", "prepare_workflow"}),
    ([], set(), {"get_project_storage", "create_environment_build", "prepare_workflow"}),
])
def test_server_disabled_modules_are_not_registered(monkeypatch, enabled, required, omitted):
    _client, servers = startup(monkeypatch, enabled)
    facade.main()
    names = tool_names(servers[0])
    assert required <= names and not names & omitted and "get_project_context" in names


@pytest.mark.parametrize("selected,expected", [("storage,workflows", ("storage", "workflows")),
    ("", ()), ("storage,environments", ("storage",))])
def test_explicit_selection_only_narrows_authenticated_server_modules(monkeypatch, selected, expected):
    client, servers = startup(monkeypatch, ("storage", "workflows"))
    monkeypatch.setenv("BROKER_EXTENSIONS", selected)
    facade.main()
    names = tool_names(servers[0])
    assert ("get_project_storage" in names) == ("storage" in expected)
    assert ("prepare_workflow" in names) == ("workflows" in expected)
    assert "create_environment_build" not in names
    assert client.calls == [("GET", "/api/v1/context"), ("GET", "/api/v1/capabilities")]


@pytest.mark.parametrize("document", [[], {}, {"api_version": "2.0", "enabled_extensions": []},
    {"api_version": "1.0", "enabled_extensions": None},
    {"api_version": "1.0", "enabled_extensions": "storage"},
    {"api_version": "1.0", "enabled_extensions": [True]},
    {"api_version": "1.0", "enabled_extensions": ["storage", "storage"]},
    {"api_version": "1.0", "enabled_extensions": ["storage,workflows"]},
    {"api_version": "1.0", "enabled_extensions": ["secret_private_arbitrary.module"]},
])
def test_malformed_capabilities_fail_startup_without_silent_core_downgrade_or_output(monkeypatch, capsys, document):
    _client, servers = startup(monkeypatch, document=document)
    monkeypatch.setenv("BROKER_EXTENSIONS", "")
    with pytest.raises(SystemExit) as error:
        facade.main()
    assert error.value.code == 1 and servers == []
    output = capsys.readouterr()
    assert output.out == "" and "capabilities" in output.err and "startup failed" in output.err
    assert "secret_private" not in output.err and "cbp_private" not in output.err


@pytest.mark.parametrize("failure", [ControlError("secret_private", 401), ControlError("secret_private", 403),
    ControlError("secret_private", 500), OSError("secret_private_url_or_key_path"),
    RuntimeError("secret_private_connection_failure")])
def test_authentication_or_connection_failure_is_stderr_only_and_starts_no_server(monkeypatch, capsys, failure):
    _client, servers = startup(monkeypatch, failure=failure)
    with pytest.raises(SystemExit) as error:
        facade.main()
    assert error.value.code == 1 and servers == []
    output = capsys.readouterr()
    assert output.out == "" and "startup failed" in output.err
    assert "secret_private" not in output.err and "cbp_private" not in output.err


def test_capability_query_failure_does_not_downgrade_after_context_succeeds(monkeypatch, capsys):
    client, servers = startup(monkeypatch)
    original = client.request
    def request(method, path):
        if path == "/api/v1/capabilities":
            raise ControlError("secret_private_capability_response", 403)
        return original(method, path)
    monkeypatch.setattr(client, "request", request)
    with pytest.raises(SystemExit): facade.main()
    output = capsys.readouterr()
    assert servers == [] and output.out == "" and "jobs:read" in output.err
    assert "secret_private" not in output.err


def test_programmatic_facade_stays_core_only_without_configuration(monkeypatch):
    client, _servers = startup(monkeypatch)
    project = facade.ProjectFacade.from_credential(client)
    assert project.enabled_extensions == ()
    assert "prepare_workflow" not in tool_names(facade.create_server(project))
    assert client.calls == [("GET", "/api/v1/context")]
