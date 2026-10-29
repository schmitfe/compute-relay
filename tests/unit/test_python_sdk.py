"""The SDK shares MCP implementation and preserves the broker's review boundary."""

import json
from types import SimpleNamespace
import time

import pytest

from compute_relay import sdk
from compute_relay.control.auth import token_hash
from compute_relay.control.models import Attempt, ExecutionRequest
from compute_relay.mcp import facade, setup, workflow
from compute_relay import workspace, workspace_completion
from compute_relay.worker import client
from test_support.unit_test_control import broker, prepared_job
from test_support.unit_test_project_context import context_live_server


TOKEN = "cbp_synthetic_sdk_project_credential_123456789"


def test_sdk_exports_the_existing_implementations():
    assert sdk.WorkspaceClient is workspace.WorkspaceClient
    assert sdk.ProjectFacade is facade.ProjectFacade
    assert sdk.ControlError is client.ControlError
    assert sdk.finish is workspace_completion.finish


def test_generated_sdk_script_waits_locally_and_exports_without_acknowledging(tmp_path, monkeypatch):
    job = {"id": "synthetic-job", "review_url": "/review/synthetic-job/"}
    local = SimpleNamespace(client=object(), stage=lambda paths: {"bundle_id": "synthetic-bundle"},
        prepare=lambda bundle, path: job)
    waits, sleeps, exports, execution = [], [], [], []
    responses = iter([sdk.ControlError("CONTROL_HTTP_ERROR", 429, broker_code="WAIT_BUSY"),
        {**job, "revision": "a" * 64, "completion": {"operationally_reconciled": False},
            "next_action": {"action": "wait"}},
        {**job, "revision": "b" * 64, "completion": {"operationally_reconciled": True}}])
    def get_job(identifier, **options):
        waits.append(options)
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response
    project = SimpleNamespace(request_execution=execution.append, get_job=get_job)
    monkeypatch.setattr(sdk, "WorkspaceClient", lambda path: local)
    monkeypatch.setattr(sdk, "ProjectFacade", SimpleNamespace(from_credential=lambda client: project))
    monkeypatch.setattr(sdk, "finish", lambda workspace, **options: exports.append(options))
    monkeypatch.setattr(time, "sleep", sleeps.append)
    guide = workflow.usage_guide(tmp_path, "/installed/python").decode()
    script = guide.split("```python\n", 1)[1].split("\n```", 1)[0]
    exec(compile(script, "generated-sdk-example", "exec"), {})
    assert execution == [job["id"]] and sleeps == [1]
    assert waits == [{"after_revision": None, "wait_seconds": 0},
        {"after_revision": None, "wait_seconds": 0}, {"after_revision": "a" * 64, "wait_seconds": 20}]
    assert exports == [{"job_id": job["id"], "output": "results/" + job["id"], "resume": True}]


@pytest.mark.django_db(transaction=True)
def test_generated_sdk_script_uses_real_http_and_cannot_approve_execution(
        broker, context_live_server, tmp_path, capsys):
    broker.credential.token_hash = token_hash(TOKEN)
    broker.credential.save(update_fields=["token_hash"])
    root = setup.initialize(tmp_path / "science 'quoted'", api_key=TOKEN,
        broker_url=context_live_server.url)
    source = b"raise RuntimeError('source-must-not-execute-locally')\n"
    (root / "main.py").write_bytes(source)
    request = dict(prepared_job(broker).request)
    request.pop("project_id")
    request.pop("files")
    (root / "job.json").write_text(json.dumps(request))
    guide = (root / workflow.USAGE_PATH).read_text()
    script = guide.split("```python\n", 1)[1].split("\n```", 1)[0]
    for _ in range(2):
        exec(compile(script, "generated-sdk-example", "exec"), {})
    pending = ExecutionRequest.objects.get()
    assert pending.state == "awaiting_approval"
    assert pending.reservation_state == "held"
    assert not Attempt.objects.exists()
    assert not (root / "results").exists()
    output = capsys.readouterr().out
    assert "await_human_review" in output and "/review/" in output
    assert TOKEN not in output and source.decode().strip() not in output
