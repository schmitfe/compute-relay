"""MCP requests install real fixture wheels in disposable compute directories.

The fixture compute runner executes only the exact reviewed first-party build
and inventory programs. Neither the broker nor worker executes submitted code.
No institutional cluster, credentials, registry access or network installs.
"""

import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import zipfile

from django.test import Client
from django.utils import timezone
import pytest

from compute_relay.backends import SlurmBackend
from compute_relay.control import services as core
from compute_relay.control.models import Attempt, Environment, Job, LedgerEntry
from compute_relay.extensions.environments import inspection, rendering, services
from compute_relay.extensions.environments.inventory import INSPECTION_SCRIPT
from compute_relay.extensions.environments.models import EnvironmentBuild, EnvironmentInspection
from compute_relay.extensions.environments.remote import REMOTE_SOURCE
from compute_relay.mcp.facade import ProjectFacade, create_server
from compute_relay.remote import helper
from compute_relay.worker.journal import Journal
from compute_relay.worker.runner import Worker
from test_environment_extension import context


class HTTP:
    def __init__(self, token):
        self.client = Client(HTTP_AUTHORIZATION="Bearer " + token)
    def request(self, method, path, body=None, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._request(method, path, body, **kwargs)
        # Bridge an MCP event loop to the synchronous in-process HTTP fixture.
        from concurrent.futures import ThreadPoolExecutor
        def call():
            from django.db import connections
            try:
                return self._request(method, path, body, **kwargs)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(call).result()

    def _request(self, method, path, body=None, **kwargs):
        response = self.client.generic(method, path, data=json.dumps(body or {}), content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=kwargs.get("idempotency_key", ""))
        assert response.status_code < 400, (response.status_code, response.content[:500])
        return response.json()


def wheel(version):
    buffer = io.BytesIO()
    info = f"relay_fixture-{version}.dist-info"
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("relay_fixture/__init__.py", f"__version__ = {version!r}\n")
        archive.writestr(info + "/METADATA", f"Metadata-Version: 2.1\nName: relay-fixture\nVersion: {version}\n")
        archive.writestr(info + "/WHEEL", "Wheel-Version: 1.0\nGenerator: relay-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(info + "/RECORD", "")
    content = buffer.getvalue()
    return {"name": "relay-fixture", "version": version, "filename": f"relay_fixture-{version}-py3-none-any.whl",
        "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}, content


def invoke(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


@pytest.mark.django_db(transaction=True)
def test_agent_inspects_builds_and_updates_usable_environment_without_per_build_promotion(context, tmp_path, monkeypatch):
    c = context
    c.base.name = "misleading-Python-3.7-label"
    c.base.configuration = {"interpreter": sys.executable}
    c.base.save()
    c.connection.configuration = {**c.connection.configuration, "remote_root": str(tmp_path / "compute")}
    c.connection.save()
    services.update_policy(c.project, c.owner, {**c.policy.configuration, "auto_permit_builds": True})
    public = HTTP("environment-agent")
    facade = ProjectFacade(public, str(c.project.id), enabled_extensions=["environments"])
    server = create_server(facade)
    def scheduler(*args, **kwargs):
        return {"returncode": 0, "stdout": "42\n", "stderr": ""}
    monkeypatch.setattr(helper, "scheduler", scheduler)
    class Transport:
        def call_helper(self, source, request):
            if source == REMOTE_SOURCE:
                result = subprocess.run([sys.executable, "-I", "-c", source], input=json.dumps(request),
                    text=True, capture_output=True, check=True, timeout=30)
                return json.loads(result.stdout)
            return helper.execute(request["config"], request)
    connection = {**c.connection.configuration, "backend": "slurm", "revision_sha256": c.connection.revision_sha256}
    backend = SlurmBackend(connection, transport=Transport())
    worker = Worker(HTTP("environment-worker"), {str(c.connection.id): connection}, Journal(tmp_path / "journal"),
        backend_factory=lambda _: backend, extensions=["environments"])
    backend.observe = lambda *a, **kw: {"state": "completed", "terminal": True,
        "accounting": {"final": True, "cpu_core_seconds": 1, "gpu_device_seconds": 0}}

    def compute(attempt_id):
        lease = core.lease_work(c.worker)["lease"]
        assert lease["attempt_id"] == attempt_id and lease["operation"] == "submit"
        worker.execute(lease)
        attempt = Attempt.objects.get(id=attempt_id)
        directory = Path(attempt.native_identity["work_dir"])
        script = attempt.job.request["command"]["script"]
        if script == "_environment/runtime_inventory.py":
            assert (directory / script).read_text() == INSPECTION_SCRIPT
            executable = attempt.job.request["environment"]["id"]
            python = Environment.objects.get(id=executable).configuration["interpreter"]
        else:
            build = EnvironmentBuild.objects.get(job=attempt.job)
            expected = rendering.render_build_files(build.spec.document, build_id=build.id, spec_sha256=build.spec.sha256,
                policy=build.policy.configuration)[script]
            assert (directory / script).read_bytes() == expected
            python = sys.executable
        # Disposable fixture compute node, outside broker and worker execution.
        result = subprocess.run([python, str(directory / script)], cwd=directory, capture_output=True, timeout=90)
        assert result.returncode == 0, result.stderr[-2000:]
        (directory / "stdout.log").write_bytes(result.stdout)
        (directory / "stderr.log").write_bytes(result.stderr)
        Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now())
        worker.execute(core.lease_work(c.worker)["lease"])
        return Attempt.objects.get(id=attempt.id)

    first_inspection = invoke(server, "inspect_environment", {"environment_id": str(c.base.id), "idempotency_key": "initial-inventory"})
    resumed = invoke(server, "inspect_environment", {"environment_id": str(c.base.id), "idempotency_key": "recovered-inventory"})
    assert resumed["id"] == first_inspection["id"] and resumed["execution"]["attempt_id"] == first_inspection["execution"]["attempt_id"]
    compute(first_inspection["execution"]["attempt_id"])
    observed = invoke(server, "get_environment_inventory", {"environment_id": str(c.base.id)})
    assert observed["registered_name"] == "misleading-Python-3.7-label"
    assert observed["runtime"]["version"] == sys.version.split()[0]
    assert observed["summary"]["python_version"] != "3.7"
    assert observed["latest_inspection"]["observation"]["scope"] == "compute_node"

    def package_request(environment, version, key):
        descriptor, content = wheel(version)
        public.request("POST", f"/api/v1/projects/{c.project.id}/objects", {"path": descriptor["filename"],
            "content_base64": base64.b64encode(content).decode()})
        arguments = {"environment_id": str(environment.id), "packages": [descriptor], "idempotency_key": key}
        requested = invoke(server, "request_environment_packages", arguments)
        replay = invoke(server, "request_environment_packages", arguments)
        assert replay["build"]["id"] == requested["build"]["id"]
        compute(requested["execution"]["attempt_id"])
        build = EnvironmentBuild.objects.get(id=requested["build"]["id"])
        assert build.state == "ready", (build.state, build.error_code)
        inventory = invoke(server, "get_environment_inventory", {"environment_id": str(build.environment_id)})
        assert {"name": "relay-fixture", "version": version} in inventory["runtime"]["packages"]
        c.grant.refresh_from_db()
        assert build.environment.revision_sha256 in c.grant.policy["allowed_environment_revision_sha256"]
        return build

    first = package_request(c.base, "1.0", "package-v1")
    cached = invoke(server, "request_environment_packages", {"environment_id": str(c.base.id),
        "packages": first.spec.document["requirements"], "idempotency_key": "reuse-v1"})
    assert cached["build"]["cache_hit"] and cached["execution"] is None
    second = package_request(first.environment, "2.0", "package-v2")
    assert second.parent_environment_id == first.environment_id and second.environment_id != first.environment_id
    assert second.spec.document["base_environment"]["id"] == str(c.base.id)
    for build, expected in ((first, "1.0"), (second, "2.0")):
        result = subprocess.run([build.environment.configuration["interpreter"], "-I", "-c",
            "import relay_fixture; print(relay_fixture.__version__)"], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == expected
    assert core.allowance(c.project)["jobs"]["admitted"] == 3
    assert LedgerEntry.objects.filter(action="settle").count() == 3
    assert not core.allowance(c.project)["reserved"]["cpu_core_seconds"]
    assert not c.project.environments.get(id=first.environment_id).revision_sha256 == second.environment.revision_sha256

    # External mutation cannot silently acquire a new registered revision.
    prefix = Path(second.environment.configuration["prefix"])
    metadata = next(prefix.glob("lib/python*/site-packages/relay_fixture-2.0.dist-info/METADATA"))
    metadata.write_text(metadata.read_text().replace("Version: 2.0", "Version: 9.0"))
    drift = invoke(server, "inspect_environment", {"environment_id": str(second.environment_id), "idempotency_key": "detect-drift"})
    compute(drift["execution"]["attempt_id"])
    observed = invoke(server, "get_environment_inventory", {"environment_id": str(second.environment_id)})
    assert observed["summary"]["matches_managed_receipt"] is False
    assert observed["registered_revision_sha256"] == second.environment.revision_sha256
