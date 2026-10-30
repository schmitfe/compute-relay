"""Investigation scope, accounting, recovery and verified local closeout."""

import asyncio
from copy import deepcopy
import json
from uuid import uuid4

import pytest
from django.test import Client

from compute_relay.control import services as core
from compute_relay.control.auth import token_hash
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Artifact, Attempt, Job, Project, ResultAccess
from compute_relay.extensions.investigations import services
from compute_relay.extensions.investigations.models import Investigation, InvestigationJob
from compute_relay.mcp.facade import ProjectFacade, create_server
from compute_relay.worker.client import ControlError
from compute_relay.workspace import WorkspaceClient
from compute_relay.workspace_completion import finish
from test_support.unit_test_control import publish_output, broker, budgeted, dispatched, execution, prepared_job, submit_receipt
from test_support.unit_test_workspace import workspace


@pytest.fixture
def planned(broker):
    broker.credential.scopes += ["investigations:write"]
    broker.credential.save(update_fields=["scopes"])
    document = {"name": "bounded-science", "plan": {"question": "Does the approximation converge?",
        "deliverable": "A convergence table and uncertainty statement", "core_experiments": ["pilot", "two resolutions"],
        "required_validation": ["residual below the planned tolerance"], "stopping_rule": "Finish after core and validation; otherwise report inconclusive after one diagnostic.",
        "budget": {"cpu_core_seconds": 1000, "gpu_device_seconds": 0, "max_executions": 4}, "diagnostic_limit": 1}}
    result, _ = services.create(broker.credential, document)
    return broker, Investigation.objects.get(id=result["id"]), document


class HTTP:
    def __init__(self, broker):
        self.client = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
        self.calls = []

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path))
        response = self.client.generic(method, path, data=json.dumps(body or {}), content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=kwargs.get("idempotency_key", "synthetic-" + uuid4().hex))
        if response.status_code >= 400:
            raise ControlError(response.json()["error"]["code"], response.status_code)
        return response.json()

    def get_bytes(self, path, maximum):
        response = self.client.get(path)
        assert response.status_code == 200
        data = b"".join(response.streaming_content) if response.streaming else response.content
        assert len(data) <= maximum
        return data, response["Content-Type"]


def attach(context, group, job, role="core"):
    services.bind_job(job, context.credential, {"id": str(group.id), "role": role})


def completed(context, group):
    budgeted(context)
    job = prepared_job(context, outputs=["answer.json"])
    attach(context, group, job)
    attempt, lease = dispatched(context, job)
    native, lease = submit_receipt(context, attempt, lease)
    publish_output(lease, "answer.json", b'{"answer":42}\n')
    core.report(context.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "outcome": "observed", "native_identity": native, "collection_state": "complete",
        "observation": {"state": "completed", "terminal": True,
            "accounting": {"final": True, "cpu_core_seconds": 3, "gpu_device_seconds": 0}}})
    return job, attempt


def test_grouping_is_metadata_only_and_revisions_inherit(planned):
    context, group, _ = planned
    job = prepared_job(context)
    facade = ProjectFacade(HTTP(context), str(context.project.id))
    result = facade.prepare_job(job.request, "planned-stage", investigation_id=str(group.id), role="validation")
    created = Job.objects.get(id=result["id"])
    assert InvestigationJob.objects.get(job=created).role == "validation"
    assert created.extension_name == "" and not Attempt.objects.exists()
    revised, _ = core.create_job(context.credential, created.request, previous=created)
    assert InvestigationJob.objects.get(job_id=revised["id"]).investigation_id == group.id
    found = facade.list_project_jobs("all", investigation_id=str(group.id))
    assert {row["id"] for row in found["jobs"]} == {str(created.id), revised["id"]}


def test_http_plan_attach_finish_and_idempotent_replay(planned):
    context, group, document = planned
    facade = ProjectFacade(HTTP(context), str(context.project.id))
    created = facade.client.request("POST", f"/api/v1/projects/{context.project.id}/investigations", document)
    job = prepared_job(context)
    assert facade.add_investigation_jobs(created["id"], [str(job.id)], "validation", "attach")["accepted"]
    first = facade.finish_investigation(created["id"], "sufficient", "Validation complete.", True, "close")
    assert first["state"] == "closed" and first["execution_cancelled"] is False
    assert facade.finish_investigation(created["id"], "sufficient", "Validation complete.", True, "close") == first
    assert facade.get_investigation(created["id"])["summary"]["operationally_reconciled"]


def test_official_mcp_exposes_typed_plan_and_creates_metadata_only(planned):
    context, group, document = planned
    facade = ProjectFacade(HTTP(context), str(context.project.id), enabled_extensions=["investigations"])
    server = create_server(facade)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schema = tools["create_investigation"].inputSchema
    budget = schema["$defs"]["Budget"]["properties"]
    assert budget["cpu_core_seconds"]["type"] == "integer"
    assert budget["max_executions"]["maximum"] == 10000
    assert {"get_investigation", "list_investigations", "add_investigation_jobs", "finish_investigation"} <= tools.keys()
    # Direct dispatch is synchronous because the HTTP bridge uses this thread's
    # disposable SQLite connection; list_tools above exercises SDK schema output.
    registered = server._tool_manager.get_tool("create_investigation")
    from compute_relay.extensions.investigations.mcp import InvestigationPlan
    result = registered.fn(name="MCP bounded plan", plan=InvestigationPlan.model_validate(document["plan"]))
    assert result["state"] == "open" and Investigation.objects.count() == 2
    assert not Attempt.objects.exists() and not Job.objects.exists()


def test_scope_and_foreign_context_fail_without_orphan_job(planned):
    context, group, _ = planned
    job = prepared_job(context)
    context.credential.scopes.remove("investigations:write")
    context.credential.save()
    before = Job.objects.count()
    with pytest.raises(ControlError) as error:
        ProjectFacade(HTTP(context), str(context.project.id)).prepare_job(job.request, "no-scope", investigation_id=str(group.id))
    assert error.value.status == 403 and Job.objects.count() == before
    context.credential.scopes += ["investigations:write"]
    context.credential.save()
    foreign = Investigation.objects.create(project=Project.objects.create(owner=context.owner, name="foreign"), name="foreign", plan=group.plan)
    with pytest.raises(ControlError) as error:
        ProjectFacade(HTTP(context), str(context.project.id)).prepare_job(job.request, "foreign", investigation_id=str(foreign.id))
    assert error.value.status == 404 and Job.objects.count() == before


def test_closed_scope_cancels_drafts_and_reviews_and_keeps_uncertain_work_visible(planned):
    context, group, _ = planned
    draft, pending, admitted = [prepared_job(context) for _ in range(3)]
    for job in (draft, pending, admitted):
        attach(context, group, job)
    review = execution(context, pending)
    budgeted(context)
    uncertain = execution(context, admitted).attempt
    Attempt.objects.filter(id=uncertain.id).update(dispatch_started=True, state="submission_uncertain")
    services.finish(context.credential, group, {"conclusion": "user_stopped", "report": "Stopped with uncertainty retained.", "cancel_unstarted": True})
    draft.refresh_from_db();review.refresh_from_db();uncertain.refresh_from_db()
    assert draft.state == "cancelled" and review.state == "cancelled"
    assert uncertain.accounting_state == "reserved" and uncertain.state == "submission_uncertain"
    group.refresh_from_db()
    snapshot = services.snapshot(context.credential, group)
    assert snapshot["state"] == "closed" and snapshot["summary"]["operationally_reconciled"] is False
    with pytest.raises(BrokerError, match="scope is closed"):
        core.request_execution(admitted, context.credential)


def test_status_separates_client_approval_request_and_compute_submission(planned):
    context, group, _ = planned
    job = prepared_job(context)
    api = HTTP(context)
    data = api.request("GET", f"/api/v1/jobs/{job.id}")
    assert data["execution_progress"] == {"execution_request_exists": False, "compute_submitted": False,
        "submission_uncertain": False, "dispatch_started": False, "client_permission_review": "not_observed_by_broker"}
    pending = execution(context, job)
    data = api.request("GET", f"/api/v1/jobs/{job.id}")
    assert data["execution_progress"]["execution_request_exists"] and not data["execution_progress"]["compute_submitted"]
    assert data["completion"]["pending_approvals"] == 1
    budgeted(context)
    core.cancel_execution_request(pending)
    attempt = execution(context, job).attempt
    Attempt.objects.filter(id=attempt.id).update(dispatch_started=True, state="submission_uncertain")
    data = api.request("GET", f"/api/v1/jobs/{job.id}")
    assert data["execution_progress"]["compute_submitted"] is None
    assert data["execution_progress"]["submission_uncertain"]


def test_export_downloads_verifies_and_acknowledges_without_starting_compute(planned, tmp_path):
    context, group, _ = planned
    context.token = "cbp_synthetic_investigation_workspace_123456789"
    context.credential.token_hash = token_hash(context.token);context.credential.save()
    job, attempt = completed(context, group)
    root = workspace(tmp_path / "science", token=context.token)
    api = HTTP(context)
    helper = WorkspaceClient(root, client=api)
    before = Attempt.objects.count()
    result = finish(helper, investigation_id=str(group.id), output="results/investigation", acknowledge=True)
    manifest = json.loads((root / result["manifest"]).read_text())
    record = manifest["jobs"][0]["attempts"][0]
    assert result["summary"]["spent"] == {"cpu_core_seconds": 3, "gpu_device_seconds": 0}
    assert result["summary"]["reserved"]["cpu_core_seconds"] == 0
    assert result["summary"]["operationally_reconciled"] and result["summary"]["local_delivery_complete"]
    assert record["local_delivery"]["state"] == "verified" and record["delivery"]["acknowledged"]
    assert manifest["scientific_validation"] == "not_checked_by_exporter"
    assert ResultAccess.objects.get(credential=context.credential, attempt=attempt).acknowledged_at
    assert Attempt.objects.count() == before
    assert not any("/execution-requests" in path for _, path in api.calls)
    resumed = finish(helper, investigation_id=str(group.id), output="results/investigation", resume=True, acknowledge=True)
    assert resumed["summary"]["local_delivery_complete"]


def test_metadata_only_export_does_not_claim_verification_or_ack(planned, tmp_path):
    context, group, _ = planned
    context.token = "cbp_synthetic_metadata_workspace_123456789"
    context.credential.token_hash = token_hash(context.token);context.credential.save()
    job, attempt = completed(context, group)
    root = workspace(tmp_path / "science", token=context.token)
    result = finish(WorkspaceClient(root, client=HTTP(context)), job_id=str(job.id), output="summary", download=False, acknowledge=True)
    assert result["summary"]["operationally_reconciled"]
    assert not result["summary"]["local_delivery_complete"]
    assert not ResultAccess.objects.filter(credential=context.credential, attempt=attempt, acknowledged_at__isnull=False).exists()


def test_missing_outputs_remain_visible_and_unacknowledged(planned, tmp_path):
    context, group, _ = planned
    context.token = "cbp_synthetic_missing_output_123456789"
    context.credential.token_hash = token_hash(context.token); context.credential.save()
    job, attempt = completed(context, group)
    Artifact.objects.filter(attempt=attempt).delete()
    root = workspace(tmp_path / "science", token=context.token)
    result = finish(WorkspaceClient(root, client=HTTP(context)), job_id=str(job.id), output="missing", acknowledge=True)
    assert not result["summary"]["operationally_reconciled"] and not result["summary"]["local_delivery_complete"]
    record = json.loads((root / result["manifest"]).read_bytes())["jobs"][0]
    assert record["summary"]["blockers"][0]["code"] == "OUTPUTS_MISSING"
    assert not ResultAccess.objects.filter(attempt=attempt, acknowledged_at__isnull=False).exists()


def test_exhausted_collection_retries_prevent_reconciled_claim_and_ack(planned, tmp_path):
    context, group, _ = planned
    context.token = "cbp_synthetic_collection_failure_123456789"
    context.credential.token_hash = token_hash(context.token); context.credential.save()
    job, attempt = completed(context, group)
    Attempt.objects.filter(id=attempt.id).update(collection_state="failed", collection_pending=False)
    root = workspace(tmp_path / "science", token=context.token)
    result = finish(WorkspaceClient(root, client=HTTP(context)), job_id=str(job.id), output="partial", acknowledge=True)
    assert not result["summary"]["operationally_reconciled"] and result["summary"]["local_delivery_complete"]
    record = json.loads((root / result["manifest"]).read_bytes())["jobs"][0]
    assert any(item["code"] == "COLLECTION_FAILED" for item in record["summary"]["blockers"])
    assert not ResultAccess.objects.filter(attempt=attempt, acknowledged_at__isnull=False).exists()


def test_group_counts_pending_review_and_shared_diagnostics(planned):
    context, group, _ = planned
    group.plan["budget"]["max_executions"] = 1
    group.save()
    job = prepared_job(context)
    attach(context, group, job, "exploration")
    execution(context, job)
    summary = services.snapshot(context.credential, group)["summary"]
    assert summary["attempts"] == 0 and summary["pending_approvals"] == 1
    assert summary["plan_limit_reached"] and summary["diagnostic_limit_reached"]


def test_status_wait_rechecks_revoked_credential(planned, monkeypatch):
    from compute_relay.control import completion
    context, group, _ = planned
    job = prepared_job(context)
    client = Client(HTTP_AUTHORIZATION="Bearer " + context.token)
    path = f"/api/v1/jobs/{job.id}"
    token = client.get(path).json()["revision"]
    def revoke(_):
        context.credential.revoked = True; context.credential.save()
    monkeypatch.setattr(completion.time, "sleep", revoke)
    response = client.get(path, {"after_revision": token, "wait_seconds": 1})
    assert response.status_code == 401


def test_export_resume_rejects_another_subject_and_symlink(planned, tmp_path):
    context, group, _ = planned
    context.token = "cbp_synthetic_resume_output_123456789"
    context.credential.token_hash = token_hash(context.token); context.credential.save()
    job, attempt = completed(context, group)
    root = workspace(tmp_path / "science", token=context.token)
    helper = WorkspaceClient(root, client=HTTP(context))
    result = finish(helper, job_id=str(job.id), output="output")
    original = (root / result["manifest"]).read_bytes()
    with pytest.raises(ValueError):
        finish(helper, investigation_id=str(group.id), output="output", resume=True)
    assert (root / result["manifest"]).read_bytes() == original
    (root / "linked").symlink_to(root / "output", target_is_directory=True)
    with pytest.raises(ValueError, match="Symbolic"):
        finish(helper, job_id=str(job.id), output="linked", resume=True)


def test_paginated_export_retries_changed_snapshot_then_refuses_unstable_read():
    from compute_relay.workspace_completion import _job_manifest
    identifier = str(uuid4())
    class Changing:
        calls = 0
        def get_job_completion(self, job_id, cursor, limit, execution_cursor=0):
            self.calls += 1
            return {"job": {"id": identifier}, "snapshot_revision": ("a" if self.calls == 1 else "b") * 64,
                "attempts": [{"id": str(cursor)}], "execution_requests": [], "next_cursor": 20 if cursor == 0 and limit == 20 else None}
    facade = Changing()
    result = _job_manifest(facade, identifier)
    assert result["snapshot_revision"] == "b" * 64 and len(result["attempts"]) == 2 and facade.calls == 5
    class Unstable:
        calls = 0
        def get_job_completion(self, *args, **kwargs):
            self.calls += 1
            return {"job": {"id": identifier}, "snapshot_revision": f"{self.calls:064x}", "attempts": [], "execution_requests": [], "next_cursor": None}
    with pytest.raises(ValueError, match="changed during export"):
        _job_manifest(Unstable(), identifier)


@pytest.mark.parametrize("mutation", ["boolean_budget", "unbounded_diagnostics", "no_core", "unknown"])
def test_invalid_plan_cannot_create_metadata(planned, mutation):
    context, group, document = planned
    changed = deepcopy(document)
    if mutation == "boolean_budget": changed["plan"]["budget"]["max_executions"] = True
    elif mutation == "unbounded_diagnostics": changed["plan"]["diagnostic_limit"] = 1000
    elif mutation == "no_core": changed["plan"]["core_experiments"] = []
    else: changed["plan"]["automatic_execution"] = True
    with pytest.raises(BrokerError):
        services.create(context.credential, changed)
    assert Investigation.objects.count() == 1


def test_completion_export_collects_execution_pages_without_attempts(planned):
    from compute_relay.control.models import ExecutionRequest
    from compute_relay.workspace_completion import _job_manifest
    context, _, _ = planned
    job = prepared_job(context)
    rows = [ExecutionRequest(job=job, credential=context.credential) for _ in range(45)]
    ExecutionRequest.objects.bulk_create(rows)
    facade = ProjectFacade(HTTP(context), str(context.project.id))
    result = _job_manifest(facade, str(job.id))
    assert result["attempts"] == [] and len(result["execution_requests"]) == 45
    assert {row["id"] for row in result["execution_requests"]} == {str(row.id) for row in rows}
    assert result["summary"]["pending_approvals"] == 45
    assert result["next_cursor"] is None and result["next_execution_cursor"] is None
