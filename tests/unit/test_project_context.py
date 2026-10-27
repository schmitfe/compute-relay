"""API-key identity discovery and a real stdio client against a disposable broker."""

import asyncio
import base64
from datetime import timedelta
import json
from pathlib import Path
import sys
from uuid import uuid4

from django.test import Client
from django.utils import timezone
import pytest

from compute_relay.control.models import Job, Project, ProjectCredential
from compute_relay.control.auth import token_hash
from compute_relay.mcp.facade import ProjectFacade
from compute_relay.worker.client import ControlError, JsonClient
from test_control import broker  # noqa: F401


@pytest.fixture
def context_live_server(settings, request):
    settings.STATIC_URL = "/static/"
    return request.getfixturevalue("live_server")


def test_context_returns_only_current_keys_project_and_public_metadata(broker):
    c = broker
    Project.objects.create(name="other private project", owner=c.outsider)
    response = Client().get("/api/v1/context", {"project_id": str(uuid4())},
                            HTTP_AUTHORIZATION="Bearer " + c.token)
    assert response.status_code == 200
    result = response.json()
    assert result.pop("compute")["per_job_limits"]["cpu_cores"] == 8
    assert result.pop("allowance")["project_id"] == str(c.project.id)
    assert result == {"schema_version": "1.0",
        "project": {"id": str(c.project.id), "name": c.project.name},
        "credential": {"id": str(c.credential.id), "scopes": c.credential.scopes,
                       "execution_mode": "inherit"}}
    assert response["Cache-Control"] == "no-store"
    assert "Authorization" in response["Vary"]
    assert c.token.encode() not in response.content
    assert c.credential.token_hash.encode() not in response.content
    assert b"other private project" not in response.content


def test_context_does_not_require_or_grant_a_job_scope(broker):
    c = broker
    c.credential.scopes = ["objects:write"]
    c.credential.save(update_fields=["scopes"])
    client = Client(HTTP_AUTHORIZATION="Bearer " + c.token)
    response = client.get("/api/v1/context")
    assert response.status_code == 200
    assert "compute" not in response.json() and "allowance" not in response.json()
    assert client.get("/api/v1/connections").status_code == 403
    assert client.post("/api/v1/context").status_code == 405


@pytest.mark.parametrize("change, status", [("missing", 401), ("invalid", 401), ("worker", 401),
    ("expired", 401), ("revoked", 401), ("owner_disabled", 403),
    ("foreign_issuer", 403)])
def test_context_rechecks_credentials_and_issuer_authority(broker, change, status):
    c = broker
    headers = {"HTTP_AUTHORIZATION": "Bearer " + c.token}
    if change == "missing":
        headers = {}
    elif change == "invalid":
        headers["HTTP_AUTHORIZATION"] = "Bearer synthetic-invalid-token"
    elif change == "worker":
        headers["HTTP_AUTHORIZATION"] = "Bearer synthetic-worker-token"
    elif change == "expired":
        c.credential.expires_at = timezone.now() - timedelta(seconds=1)
        c.credential.save(update_fields=["expires_at"])
    elif change == "revoked":
        c.credential.revoked = True
        c.credential.save(update_fields=["revoked"])
    elif change == "owner_disabled":
        c.owner.is_active = False
        c.owner.save(update_fields=["is_active"])
    elif change == "foreign_issuer":
        c.credential.issued_by = c.outsider
        c.credential.save(update_fields=["issued_by"])
    response = Client().get("/api/v1/context", **headers)
    assert response.status_code == status
    assert "project" not in response.json()


def test_context_allows_a_viewer_key_and_isolates_a_second_projects_key(broker):
    c = broker
    c.credential.scopes = ["jobs:read"]
    c.credential.save(update_fields=["issued_by", "scopes"])
    client = Client(HTTP_AUTHORIZATION="Bearer " + c.token)
    assert client.get("/api/v1/context").json()["project"]["id"] == str(c.project.id)
    other = Project.objects.create(owner=c.outsider, name="other project")
    ProjectCredential.objects.create(project=other, issued_by=c.outsider, name="other",
        scopes=[], token_hash=token_hash("synthetic-other-key"),
        expires_at=timezone.now() + timedelta(days=1))
    response = Client(HTTP_AUTHORIZATION="Bearer synthetic-other-key").get("/api/v1/context")
    assert response.json()["project"] == {"id": str(other.id), "name": other.name}
    assert c.project.name.encode() not in response.content


@pytest.mark.django_db(transaction=True)
def test_real_stdio_session_needs_only_endpoint_and_token_file(broker, context_live_server, tmp_path):
    """No configured project UUID, direct token environment, SSH or real jobs."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    c = broker
    live_server = context_live_server
    token_file = tmp_path / "project-key"
    token_file.write_text(c.token)
    token_file.chmod(0o600)

    async def session():
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "compute_relay.mcp"],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
                 "BROKER_CONTROL_URL": live_server.url,
                 "BROKER_PROJECT_TOKEN_FILE": str(token_file), "BROKER_EXTENSIONS": ""})
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = {tool.name for tool in (await client.list_tools()).tools}
                assert "get_project_context" in tools and "retry_job_outputs" in tools and len(tools) == 15
                assert "cancel_execution_request" in tools
                assert "approve_job" not in tools

                async def tool(name, **arguments):
                    result = await client.call_tool(name, arguments)
                    assert not result.isError, result
                    return json.loads("".join(item.text for item in result.content if item.type == "text"))

                context = await tool("get_project_context")
                assert context["project"]["id"] == str(c.project.id)
                assert context["compute"]["connections"][0]["id"] == str(c.connection.id)
                assert context["allowance"]["project_id"] == str(c.project.id)
                uploaded = await tool("upload_job_file", path="main.py", content_base64=base64.b64encode(
                    b"raise RuntimeError('Synthetic package must not execute on the broker')\n").decode())
                request = {"schema_version": "1.0", "name": "no-project-uuid",
                    "connection_id": str(c.connection.id),
                    "environment": {"id": str(c.environment.id), "revision_sha256": c.environment.revision_sha256},
                    "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
                    "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
                    "slurm": {"partition": "debug", "account": "broker"}, "files": [uploaded], "outputs": []}
                prepared = await tool("prepare_job", request=request, idempotency_key="stdio-create")
                assert "request" not in prepared and "manifest" not in prepared and "files" not in prepared
                assert prepared["reservation"] == {"cpu_core_seconds": 60, "gpu_device_seconds": 0}
                await tool("request_execution", job_id=prepared["id"], idempotency_key="stdio-execute")
                summary = await tool("get_job", job_id=prepared["id"])
                assert summary["latest_execution"]["state"] == "awaiting_approval"
                assert summary["next_action"]["action"] == "await_human_review"
                assert "manifest" not in summary and "request" not in summary
                detail = await tool("get_job", job_id=prepared["id"], detail=True)
                assert detail["request"]["project_id"] == str(c.project.id)
                assert not detail["attempts"]  # Human review is still required.
                return prepared["id"]

    job_id = asyncio.run(session())
    assert Job.objects.get(id=job_id).project_id == c.project.id

    facade = ProjectFacade.from_credential(JsonClient(live_server.url, c.token))
    c.credential.revoked = True
    c.credential.save(update_fields=["revoked"])
    with pytest.raises(ControlError):
        facade.get_project_context()
    with pytest.raises(ControlError):
        ProjectFacade.from_credential(JsonClient(live_server.url, c.token))
