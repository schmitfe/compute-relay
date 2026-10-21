"""Fresh-process acceptance for a usable core with optional modules absent.

The controller uses a real, disposable SQLite database and Django HTTP views.
The transport bridge is in-process and the backend is explicitly simulated;
this test makes no scheduler, network-isolation, or credential-isolation claim.
"""

import importlib.abc
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


class DenyImports(importlib.abc.MetaPathFinder):
    def __init__(self, prefixes):
        self.prefixes = tuple(prefixes)

    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == prefix or fullname.startswith(prefix + ".") for prefix in self.prefixes):
            raise AssertionError("Forbidden module import: " + fullname)
        return None


def deny_optional_imports(*additional):
    from compute_broker.extensions import EXTENSIONS

    prefixes = [entry[0] for entry in EXTENSIONS.values()]
    sys.meta_path.insert(0, DenyImports([*prefixes, *additional]))
    return prefixes


def assert_modules_absent(prefixes):
    assert not any(name == prefix or name.startswith(prefix + ".")
                   for name in sys.modules for prefix in prefixes)


def run_probe(tmp_path, probe, *, explicit_empty=False):
    # Never inherit a running service's settings, tokens, or config references.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("BROKER_") and key != "DJANGO_SETTINGS_MODULE"}
    environment.update(PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1",
                       BROKER_DATA_DIR=str(tmp_path / "data"),
                       BROKER_DB_PATH=str(tmp_path / "control.sqlite3"),
                       BROKER_ARTIFACT_ROOT=str(tmp_path / "objects"),
                       BROKER_BOUNDARY_ROOT=str(tmp_path),
                       DJANGO_SETTINGS_MODULE="compute_broker.control.settings")
    if explicit_empty:
        environment["BROKER_EXTENSIONS"] = ""
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), probe],
                            cwd=ROOT, env=environment, capture_output=True, text=True,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_default_core_database_routes_and_mcp_job_remain_usable(tmp_path):
    result = run_probe(tmp_path, "control")
    assert result == {"extensions": [], "job_state": "succeeded", "accounting": "settled",
                      "cpu_spent": 1, "cpu_reserved": 0, "simulation": True}


def test_explicit_empty_extensions_override_ambient_test_defaults(tmp_path):
    result = run_probe(tmp_path, "control", explicit_empty=True)
    assert result["extensions"] == [] and result["job_state"] == "succeeded"


def test_worker_and_mcp_operate_without_django_or_control_imports(tmp_path):
    assert run_probe(tmp_path, "worker") == {"worker_idle": True, "core_tools_usable": True}


def control_probe():
    import asyncio
    import base64
    import contextlib
    import io
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from uuid import UUID, uuid4

    prefixes = deny_optional_imports()
    from compute_broker.cli import bootstrap_demo

    root = Path(os.environ["BROKER_BOUNDARY_ROOT"])
    demo_dir = root / "demo"
    with contextlib.redirect_stdout(io.StringIO()):
        bootstrap_demo(SimpleNamespace(output=str(demo_dir), backend="fake", mode="budgeted",
                                       fake_state_directory=str(root / "fake")))

    from django.apps import apps
    from django.conf import settings
    from django.db import connection, connections
    from django.db.migrations.recorder import MigrationRecorder
    from django.test import Client
    from django.urls import Resolver404, resolve
    from django.utils import timezone
    from compute_broker.control.models import Attempt, Grant, Job
    from compute_broker.extensions import EXTENSIONS
    from compute_broker.mcp.facade import ProjectFacade, create_server
    from compute_broker.worker.journal import Journal
    from compute_broker.worker.runner import Worker

    assert settings.BROKER_EXTENSIONS == ()
    assert not any(config.name in prefixes for config in apps.get_app_configs())
    tables = connection.introspection.table_names()
    assert "broker_attempt" in tables and "broker_grant" in tables
    assert not any(table.startswith("broker_" + name + "_")
                   for name in EXTENSIONS for table in tables)
    assert not any(label.startswith("broker_") for label, _ in MigrationRecorder(connection).applied_migrations())
    assert not any(model.__module__.startswith(tuple(prefixes)) for model in apps.get_models())
    demo = json.loads((demo_dir / "demo.json").read_text())
    project_id = demo["project_id"]
    plugin_paths = [f"/api/v1/projects/{project_id}/environment-builds",
                    f"/api/v1/projects/{project_id}/workflow-revisions",
                    f"/api/v1/projects/{project_id}/storage", "/storage/targets/"]
    for path in plugin_paths:
        try:
            resolve(path)
        except Resolver404:
            pass
        else:
            raise AssertionError("Disabled extension route resolved: " + path)

    class HttpBridge:
        """Use real HTTP handlers; no socket or deployed server is involved."""

        def __init__(self, token):
            self.client = Client(HTTP_AUTHORIZATION="Bearer " + token.strip())

        def request(self, method, path, body=None, *, idempotency_key=None):
            # The real facade uses synchronous HTTP to a separate controller.
            # Keep Django's synchronous handlers off the SDK's event-loop thread.
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(self.handle, method, path, body, idempotency_key).result()

        def handle(self, method, path, body, idempotency_key):
            headers = {"HTTP_IDEMPOTENCY_KEY": idempotency_key} if idempotency_key else {}
            try:
                if method == "GET":
                    response = self.client.get(path, **headers)
                else:
                    response = self.client.post(path, data=json.dumps(body or {}),
                                                content_type="application/json", **headers)
                assert 200 <= response.status_code < 300, (path, response.status_code, response.content)
                return response.json()
            finally:
                connections.close_all()

    facade = ProjectFacade(HttpBridge((demo_dir / "agent-token").read_text()), project_id)
    server = create_server(facade)

    def tool(name, arguments=None):
        result = asyncio.run(server.call_tool(name, arguments or {}))
        return json.loads("".join(item.text for item in result if item.type == "text"))

    tools = {item.name for item in asyncio.run(server.list_tools())}
    assert {"get_broker_capabilities", "upload_job_file", "prepare_job", "request_execution", "get_job"} <= tools
    assert not {"prepare_workflow", "start_workflow", "build_environment"} & tools
    assert tool("get_broker_capabilities")["enabled_extensions"] == []
    uploaded = tool("upload_job_file", {"path": "main.py", "content_base64": base64.b64encode(
        b"raise RuntimeError('simulation must never execute uploaded source')\n").decode()})
    request = {"schema_version": "1.0", "project_id": project_id, "connection_id": demo["connection_id"],
               "name": "core-only-acceptance", "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
               "environment": {"id": demo["environment_id"], "revision_sha256": demo["environment_revision_sha256"]},
               "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
               "slurm": {"partition": "debug", "account": "broker"}, "files": [uploaded], "outputs": ["result.json"]}
    prepared = tool("prepare_job", {"request": request, "idempotency_key": str(uuid4())})
    assert prepared["manifest"].get("extension_name", "") == ""
    execution = tool("request_execution", {"job_id": prepared["id"], "idempotency_key": str(uuid4())})
    assert execution["attempt_id"]
    worker_config = json.loads((demo_dir / "worker.json").read_text())
    worker = Worker(HttpBridge((demo_dir / "worker-token").read_text()), worker_config["connections"],
                    Journal(root / "journal"), credential_profiles=worker_config["credential_profiles"])
    # Advance only the disposable test scheduler clock; there are no sleeps or
    # real scheduler operations. Bootstrap also queues one simulated discovery.
    for _ in range(6):
        Attempt.objects.update(next_poll_at=timezone.now())
        worker.poll_once()
        attempt = Attempt.objects.get(id=execution["attempt_id"])
        if attempt.accounting_state == "settled":
            break
    detail = tool("get_job", {"job_id": prepared["id"]})
    assert len(detail["attempts"]) == 1 and Job.objects.count() == 1
    assert UUID(detail["attempts"][0]["id"]) == attempt.id
    assert attempt.observation["simulation"] is True
    assert attempt.artifacts.get(path="result.json").blob.size_bytes > 0
    grant = Grant.objects.get(project_id=project_id)
    assert_modules_absent(prefixes)
    print(json.dumps({"extensions": list(settings.BROKER_EXTENSIONS), "job_state": detail["state"],
                      "accounting": attempt.accounting_state, "cpu_spent": grant.spent_cpu,
                      "cpu_reserved": grant.reserved_cpu, "simulation": True}))


def worker_probe():
    import asyncio
    import importlib
    from uuid import uuid4

    prefixes = deny_optional_imports("django", "compute_broker.control")
    # Import every worker implementation module, including new future modules;
    # __main__ is just the executable entry point and would start its run loop.
    directory = ROOT / "src/compute_broker/worker"
    for path in sorted(directory.rglob("*.py")):
        if path.stem == "__main__":
            continue
        parts = path.relative_to(directory).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        importlib.import_module(".".join(("compute_broker", "worker", *parts)))
    from compute_broker.worker.journal import Journal
    from compute_broker.worker.runner import Worker
    from compute_broker.mcp.facade import ProjectFacade, create_server

    class OfflineClient:
        def __init__(self):
            self.calls = []

        def request(self, method, path, body=None, **kwargs):
            self.calls.append((method, path))
            if path == "/internal/v1/worker/profiles":
                return {"accepted": True}
            if path == "/internal/v1/lease":
                return {"lease": None}
            if path == "/api/v1/capabilities":
                return {"enabled_extensions": [], "scopes": ["jobs:read"]}
            raise AssertionError("Unexpected offline request: " + path)

    client = OfflineClient()
    worker = Worker(client, {}, Journal(Path(os.environ["BROKER_BOUNDARY_ROOT"]) / "journal"))
    assert worker.extensions == () and worker.poll_once() is False
    assert client.calls == [("POST", "/internal/v1/worker/profiles"), ("POST", "/internal/v1/lease")]
    server = create_server(ProjectFacade(client, str(uuid4())))
    result = asyncio.run(server.call_tool("get_broker_capabilities", {}))
    assert json.loads("".join(item.text for item in result if item.type == "text"))["enabled_extensions"] == []
    assert_modules_absent([*prefixes, "django", "compute_broker.control"])
    print(json.dumps({"worker_idle": True, "core_tools_usable": True}))


if __name__ == "__main__":
    {"control": control_probe, "worker": worker_probe}[sys.argv[1]]()
