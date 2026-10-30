"""Investigation scope, accounting, recovery and verified local closeout."""

import asyncio
from copy import deepcopy
import json
from uuid import uuid4
from types import SimpleNamespace

import pytest
from django.test import Client

from compute_relay.control import services as core
from compute_relay.control.auth import token_hash
from compute_relay.control.errors import BrokerError
from compute_relay.control.storage import store_blob, store_uploaded_blob
from compute_relay.control.models import Artifact, Attempt, Connection, Environment, Job, JobFile, MetadataUsage, Project, ProjectCredential, ResultAccess
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
        def get_job_completion(self, job_id, cursor, limit):
            self.calls += 1
            return {"job": {"id": identifier}, "snapshot_revision": ("a" if self.calls == 1 else "b") * 64,
                "attempts": [{"id": str(cursor)}], "next_cursor": 20 if cursor == 0 and limit == 20 else None}
    facade = Changing()
    result = _job_manifest(facade, identifier)
    assert result["snapshot_revision"] == "b" * 64 and len(result["attempts"]) == 2 and facade.calls == 5
    class Unstable:
        calls = 0
        def get_job_completion(self, *args):
            self.calls += 1
            return {"job": {"id": identifier}, "snapshot_revision": f"{self.calls:064x}", "attempts": [], "next_cursor": None}
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


@pytest.fixture
def two_clusters(planned):
    context, group, document = planned
    connection = Connection.objects.create(owner=context.owner, worker=context.worker, name="second", backend="fake",
        configuration=context.connection.configuration, revision_sha256="d" * 64)
    environment = Environment.objects.create(owner=context.owner, connection=connection, name="second-python",
        configuration=context.environment.configuration, revision_sha256="f" * 64)
    context.project.connections.add(connection)
    context.project.environments.add(environment)
    # The original group's scope stays at the first cluster even when project access grows.
    context.credential.allowed_connection_ids = [str(context.connection.id)]
    context.credential.save()
    other = SimpleNamespace(**vars(context))
    other.connection, other.environment = connection, environment
    other.credential, other.token = sibling_key(context, None)
    return context, group, document, other


def sibling_key(context, connection_ids):
    token = "synthetic-sibling-" + uuid4().hex
    credential = ProjectCredential.objects.create(project=context.project, issued_by=context.owner, name="sibling",
        prefix="synthetic", token_hash=token_hash(token), scopes=context.credential.scopes,
        expires_at=context.credential.expires_at, allowed_connection_ids=connection_ids)
    return credential, token


@pytest.mark.parametrize("populated", [False, True])
def test_disjoint_key_cannot_poison_or_close_investigation(two_clusters, populated):
    context, group, _, other = two_clusters
    original = prepared_job(context)
    if populated:
        attach(context, group, original)
    other.credential.allowed_connection_ids = [str(other.connection.id)]
    other.credential.save()
    foreign_job = prepared_job(other)
    attacker = ProjectFacade(HTTP(other), str(context.project.id))
    assert attacker.list_investigations()["investigations"] == []
    for operation in (
        lambda: attacker.get_investigation(str(group.id)),
        lambda: attacker.finish_investigation(str(group.id), "user_stopped", "Closed by another key.", False),
        lambda: attacker.add_investigation_jobs(str(group.id), [str(foreign_job.id)]),
        lambda: attacker.prepare_job(foreign_job.request, investigation_id=str(group.id)),
    ):
        before = Job.objects.count()
        with pytest.raises(ControlError) as error:
            operation()
        assert error.value.status == 403 and Job.objects.count() == before
    owner = ProjectFacade(HTTP(context), str(context.project.id))
    assert owner.get_investigation(str(group.id))["state"] == "open"
    assert owner.add_investigation_jobs(str(group.id), [str(original.id)])["accepted"]
    assert owner.finish_investigation(str(group.id), "sufficient", "Original access retained.")["state"] == "closed"
    assert not InvestigationJob.objects.filter(job=foreign_job).exists()


def test_broader_sibling_cannot_expand_scope_by_attachment_creation_or_revision(two_clusters):
    context, group, _, other = two_clusters
    original, foreign = prepared_job(context), prepared_job(other)
    attach(context, group, original)
    broad = ProjectFacade(HTTP(other), str(context.project.id))
    assert broad.get_investigation(str(group.id))["connection_ids"] == [str(context.connection.id)]
    for operation in (
        lambda: broad.add_investigation_jobs(str(group.id), [str(foreign.id)]),
        lambda: broad.prepare_job(foreign.request, investigation_id=str(group.id)),
        lambda: broad.revise_job(str(original.id), foreign.request),
    ):
        before = Job.objects.count()
        with pytest.raises(ControlError) as error:
            operation()
        assert error.value.status == 403 and Job.objects.count() == before
    assert list(group.members.values_list("job_id", flat=True)) == [original.id]


def test_scope_filters_before_pagination_and_applies_to_empty_groups(two_clusters):
    context, group, document, other = two_clusters
    broad = ProjectFacade(HTTP(other), str(context.project.id))
    broad_group = broad.client.request("POST", f"/api/v1/projects/{context.project.id}/investigations", document)
    narrow = ProjectFacade(HTTP(context), str(context.project.id))
    own_group = narrow.client.request("POST", f"/api/v1/projects/{context.project.id}/investigations", document)
    page = narrow.list_investigations(limit=1)
    assert [item["id"] for item in page["investigations"]] == [str(group.id)]
    assert page["next_cursor"] == 1
    page = narrow.list_investigations(cursor=1, limit=1)
    assert [item["id"] for item in page["investigations"]] == [own_group["id"]]
    assert page["next_cursor"] is None
    for operation in (
        lambda: narrow.get_investigation(broad_group["id"]),
        lambda: narrow.list_project_jobs("all", investigation_id=broad_group["id"]),
        lambda: narrow.finish_investigation(broad_group["id"], "user_stopped", "Not authorized."),
    ):
        with pytest.raises(ControlError) as error:
            operation()
        assert error.value.status == 403
    assert len(broad.list_investigations()["investigations"]) == 3


def test_scope_survives_key_rotation_and_honors_current_project_revocation(two_clusters):
    context, group, _, _ = two_clusters
    old = ProjectFacade(HTTP(context), str(context.project.id))
    credential, token = sibling_key(context, [str(context.connection.id)])
    rotated = ProjectFacade(HTTP(SimpleNamespace(token=token)), str(context.project.id))
    context.credential.revoked = True
    context.credential.save()
    with pytest.raises(ControlError) as error:
        old.get_investigation(str(group.id))
    assert error.value.status == 401
    assert rotated.get_investigation(str(group.id))["id"] == str(group.id)
    context.project.connections.remove(context.connection)
    with pytest.raises(ControlError) as error:
        rotated.get_investigation(str(group.id))
    assert error.value.status == 403 and rotated.list_investigations()["investigations"] == []
    context.project.connections.add(context.connection)
    assert rotated.get_investigation(str(group.id))["id"] == str(group.id)


@pytest.mark.parametrize("revocation", ["disabled", "archived", "worker", "owner", "key"])
def test_current_cluster_authority_filters_investigation_reads(two_clusters, revocation):
    context, group, _, _ = two_clusters
    if revocation == "worker":
        context.worker.revoked = True
        context.worker.save()
    elif revocation == "key":
        context.credential.allowed_connection_ids = []
        context.credential.save()
    elif revocation == "owner":
        context.project.owner = context.outsider
        context.project.save()
    else:
        if revocation == "disabled": context.connection.enabled = False
        else: context.connection.archived = True
        context.connection.save()
    api = ProjectFacade(HTTP(context), str(context.project.id))
    with pytest.raises(ControlError) as error:
        api.get_investigation(str(group.id))
    assert error.value.status == 403
    if revocation != "owner":
        assert api.list_investigations()["investigations"] == []


def test_scope_rows_are_accounted_and_connections_cannot_be_deleted(planned):
    from django.db.models.deletion import ProtectedError
    context, group, document = planned
    before = MetadataUsage.objects.get(kind="investigation", object_id=str(group.id)).size_bytes
    assert group.connections.get() == context.connection
    assert before >= len(json.dumps(document).encode()) + 2048 + 128
    with pytest.raises(ProtectedError):
        context.connection.delete()


def test_key_without_clusters_cannot_create_unscoped_investigation(planned):
    context, group, document = planned
    context.credential.allowed_connection_ids = []
    context.credential.save()
    api = ProjectFacade(HTTP(context), str(context.project.id))
    with pytest.raises(ControlError) as error:
        api.client.request("POST", f"/api/v1/projects/{context.project.id}/investigations", document)
    assert error.value.status == 403 and Investigation.objects.count() == 1



def test_narrowed_key_cannot_execute_member_of_broader_investigation(two_clusters):
    context, group, document, other = two_clusters
    broad = ProjectFacade(HTTP(other), str(context.project.id))
    broad_group = broad.client.request("POST", f"/api/v1/projects/{context.project.id}/investigations", document)
    job = prepared_job(context)
    broad.add_investigation_jobs(broad_group["id"], [str(job.id)])
    narrow = ProjectFacade(HTTP(context), str(context.project.id))
    with pytest.raises(ControlError) as error:
        narrow.client.request("POST", f"/api/v1/jobs/{job.id}/execution-requests", {})
    assert error.value.status == 403 and not job.execution_requests.exists()


def test_fixed_scope_covers_artifact_producers_for_all_attachment_paths(two_clusters):
    context, group, _, other = two_clusters
    original = prepared_job(context)
    attach(context, group, original)
    candidate = prepared_job(context)
    budgeted(other)
    other.grant.policy["allowed_connection_ids"].append(str(other.connection.id))
    other.grant.policy["allowed_environment_revision_sha256"].append(other.environment.revision_sha256)
    other.grant.save()
    producer = prepared_job(other)
    producer_attempt = execution(other, producer).attempt
    content = b"private result from the second cluster\n"
    artifact = Artifact.objects.create(attempt=producer_attempt, path="result.txt", blob=store_blob(context.project, content))
    request = deepcopy(original.request)
    request["files"] = [{"path": "main.py", "sha256": artifact.blob.sha256,
        "size_bytes": artifact.blob.size_bytes, "executable": False}]
    created, _ = core.create_job(other.credential, request)
    consumer = Job.objects.get(id=created["id"])
    source = consumer.files.get()
    assert source.source_kind == "artifact" and source.source_artifact_id == artifact.id
    broad = ProjectFacade(HTTP(other), str(context.project.id))
    for operation in (
        lambda: broad.add_investigation_jobs(str(group.id), [str(candidate.id), str(consumer.id)]),
        lambda: broad.prepare_job(request, investigation_id=str(group.id)),
        lambda: broad.revise_job(str(original.id), request),
    ):
        before = Job.objects.count(), JobFile.objects.count(), MetadataUsage.objects.count()
        with pytest.raises(ControlError) as error:
            operation()
        assert error.value.code == "CONNECTION_DENIED" and error.value.status == 403
        assert (Job.objects.count(), JobFile.objects.count(), MetadataUsage.objects.count()) == before
    assert list(group.members.values_list("job_id", flat=True)) == [original.id]
    narrow = ProjectFacade(HTTP(context), str(context.project.id))
    assert narrow.get_investigation(str(group.id))["id"] == str(group.id)
    # An explicit upload makes an independent copy; it no longer depends on B.
    store_uploaded_blob(context.project, content)
    with pytest.raises(ControlError) as error:
        broad.add_investigation_jobs(str(group.id), [str(consumer.id)])
    assert error.value.code == "CONNECTION_DENIED"
    copied = broad.prepare_job(request, investigation_id=str(group.id))
    assert Job.objects.get(id=copied["id"]).files.get(path="main.py").source_kind == "upload"
    assert len(narrow.get_investigation(str(group.id))["jobs"]) == 2


def test_artifact_producer_inside_fixed_scope_remains_usable(planned):
    context, group, _ = planned
    budgeted(context)
    producer = prepared_job(context)
    attempt = execution(context, producer).attempt
    artifact = Artifact.objects.create(attempt=attempt, path="result.txt", blob=store_blob(context.project, b"local result\n"))
    request = deepcopy(producer.request)
    request["files"] = [{"path": "main.py", "sha256": artifact.blob.sha256,
        "size_bytes": artifact.blob.size_bytes, "executable": False}]
    api = ProjectFacade(HTTP(context), str(context.project.id))
    created = api.prepare_job(request, investigation_id=str(group.id))
    assert Job.objects.get(id=created["id"]).files.get(path="main.py").source_kind == "artifact"
    assert api.get_investigation(str(group.id))["jobs"][0]["id"] == created["id"]
