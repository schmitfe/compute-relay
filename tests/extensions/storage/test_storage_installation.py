"""Optional installation and MCP surface boundaries."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest


def test_disabled_storage_needs_no_plugin_tables_routes_or_model_imports(tmp_path):
    source = Path(__file__).resolve().parents[3] / "src"
    env = {**os.environ, "BROKER_EXTENSIONS": "", "DJANGO_SETTINGS_MODULE": "compute_broker.control.settings",
           "PYTHONPATH": str(source), "BROKER_DATA_DIR": str(tmp_path / "disabled")}
    program = """
import sys
import django
django.setup()
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.urls import resolve, Resolver404
assert not apps.is_installed('compute_broker.extensions.storage')
assert 'compute_broker.extensions.storage.models' not in sys.modules
call_command('migrate', verbosity=0)
assert not any(name.startswith('broker_storage_') for name in connection.introspection.table_names())
try:
    resolve('/storage/targets/')
except Resolver404:
    pass
else:
    raise AssertionError('Disabled storage route was mounted')
"""
    result = subprocess.run([sys.executable, "-c", program], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_storage_worker_imports_no_django_or_database_modules(tmp_path):
    source = Path(__file__).resolve().parents[3] / "src"
    program = f"import sys; sys.path.insert(0, {str(source)!r}); import compute_broker.extensions.storage.worker; assert not any(name == 'django' or name.startswith('django.') for name in sys.modules)"
    result = subprocess.run([sys.executable, "-I", "-c", program], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_optional_mcp_tools_are_read_only_project_scoped_and_reject_paths_before_http():
    pytest.importorskip("mcp")
    from compute_broker.mcp.facade import ProjectFacade, create_server
    from compute_broker.extensions.storage.mcp import register
    from compute_broker.extensions.storage.planning import StoragePlanningError

    class Client:
        def __init__(self):
            self.calls = []

        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"advisory_only": True}

    client = Client()
    project_id = str(uuid4())
    facade = ProjectFacade(client, project_id, enabled_extensions=["storage"])
    server = create_server(facade)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert tools["plan_storage"].annotations.readOnlyHint is True
    assert tools["get_project_storage"].annotations.readOnlyHint is True
    disabled = create_server(ProjectFacade(client, project_id))
    assert "plan_storage" not in {tool.name for tool in asyncio.run(disabled.list_tools())}

    class Capture:
        def __init__(self):
            self.functions = {}

        def tool(self, **_kwargs):
            def add(function):
                self.functions[function.__name__] = function
                return function
            return add

    capture = Capture()
    register(capture, facade)
    capture.functions["get_project_storage"]()
    assert client.calls[0][0] == ("GET", f"/api/v1/projects/{project_id}/storage")
    document = {"connection_id": str(uuid4()), "estimates": [{"role": "work", "target_id": str(uuid4()),
        "peak_bytes": 10, "peak_inodes": 1, "path": "/private/elsewhere"}]}
    with pytest.raises(StoragePlanningError):
        capture.functions["plan_storage"](document)
    assert len(client.calls) == 1
