"""Optional DAG orchestration must preserve core admission and artifact authority."""

from copy import deepcopy
from datetime import timedelta
import json

import pytest
from django.db import transaction
from django.test import Client
from django.utils import timezone

from compute_relay.control import services as core
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Attempt, ExecutionRequest, Job, Project, ProjectCredential
from compute_relay.extensions.workflows import services
from compute_relay.extensions.workflows.models import WorkflowRevision, WorkflowRun
from compute_relay.extensions.workflows.validation import validate_workflow
from test_support.unit_test_control import publish_output, broker, budgeted, decide, prepared_job, submit_receipt


@pytest.fixture
def workflow_broker(broker):
    broker.credential.scopes += ["workflows:write"]
    broker.credential.save(update_fields=["scopes"])
    broker.worker.supported_extensions = ["workflows"]
    broker.worker.save(update_fields=["supported_extensions"])
    return broker


def graph(context, *, edges=True):
    request = prepared_job(context, outputs=["summary.json"]).request
    return {"name": "science", "nodes": [
        {"key": "simulate", "request": request},
        {"key": "analyze", "request": deepcopy(request), "inputs": [
            {"node": "simulate", "output": "summary.json", "path": "input/summary.json"}] if edges else []},
    ]}


def start(context, document=None):
    result, _ = services.create_revision(context.credential, document or graph(context))
    revision = WorkflowRevision.objects.get(id=result["id"])
    with transaction.atomic():
        result, _ = services.start_run(context.credential, revision)
    return WorkflowRun.objects.get(id=result["id"])


def finish(context, node, *, output=True, final=True, state="completed"):
    node.refresh_from_db()
    attempt = Attempt.objects.get(execution_request_id=node.execution_request_id)
    lease = core.lease_work(context.worker)["lease"]
    assert lease["attempt_id"] == str(attempt.id)
    core.dispatch(context.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "manifest_sha256": node.job.manifest_sha256,
        "connection_revision_sha256": context.connection.revision_sha256})
    native, lease = submit_receipt(context, attempt, lease)
    if output:
        publish_output(lease, "summary.json", b'{"mean": 0.25}\n')
    core.report(context.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "outcome": "observed", "native_identity": native,
        "observation": {"state": state, "terminal": True,
            "accounting": {"cpu_core_seconds": 1, "gpu_device_seconds": 0, "final": final}}})
    attempt.refresh_from_db()
    return attempt


def test_dag_is_prepared_without_creating_attempts(workflow_broker):
    context = workflow_broker
    run = start(context)
    assert run.nodes.count() == 2
    assert not Attempt.objects.exists() and not ExecutionRequest.objects.exists()
    assert all(node.job_id is None for node in run.nodes.all())


def test_diamond_graph_joins_only_after_both_branches_and_binds_both_outputs(workflow_broker):
    context = workflow_broker
    budgeted(context)
    request = graph(context)['nodes'][0]['request']
    run = start(context, {'name': 'split-merge', 'nodes': [
        {'key': 'start', 'request': deepcopy(request)},
        {'key': 'left', 'request': deepcopy(request), 'depends_on': ['start']},
        {'key': 'right', 'request': deepcopy(request), 'depends_on': ['start']},
        {'key': 'join', 'request': deepcopy(request), 'depends_on': ['left', 'right'], 'inputs': [
            {'node': 'left', 'output': 'summary.json', 'path': 'inputs/left.json'},
            {'node': 'right', 'output': 'summary.json', 'path': 'inputs/right.json'}]},
    ]})
    services.advance(run)
    finish(context, run.nodes.get(key='start'))
    services.advance(run)
    finish(context, run.nodes.get(key='left'))
    services.advance(run)
    assert run.nodes.get(key='join').job_id is None
    finish(context, run.nodes.get(key='right'))
    services.advance(run)
    join = run.nodes.get(key='join')
    assert {edge['path'] for edge in join.resolved_inputs} == {'inputs/left.json', 'inputs/right.json'}
    assert len({edge['producer_attempt_id'] for edge in join.resolved_inputs}) == 2
    finish(context, join)
    assert services.advance(run)['state'] == 'succeeded'


@pytest.mark.parametrize("change", ["cycle", "unknown", "code_collision", "undeclared_output", "duplicate", "cross_cluster"])
def test_invalid_graph_never_creates_a_revision(workflow_broker, change):
    document = graph(workflow_broker)
    if change == "cycle":
        document["nodes"][0]["depends_on"] = ["analyze"]
    elif change == "unknown":
        document["nodes"][0]["depends_on"] = ["missing"]
    elif change == "code_collision":
        document["nodes"][1]["inputs"][0]["path"] = "main.py"
    elif change == "undeclared_output":
        document["nodes"][1]["inputs"][0]["output"] = "secret.txt"
    elif change == "duplicate":
        document["nodes"][1]["key"] = "simulate"
    else:
        from uuid import uuid4
        document["nodes"][1]["request"]["connection_id"] = str(uuid4())
    with pytest.raises(ValueError):
        services.create_revision(workflow_broker.credential, document)
    assert not WorkflowRevision.objects.exists() and not Attempt.objects.exists()


def test_review_rejection_blocks_downstream_and_preserves_comment(workflow_broker):
    context = workflow_broker
    run = start(context)
    first = services.advance(run)
    producer = run.nodes.get(key="simulate")
    producer.refresh_from_db()
    assert first["state"] == "awaiting_approval"
    assert not run.nodes.get(key="analyze").job_id
    decide(context, producer.job, producer.execution_request, approved=False, comment="Reduce the ensemble.")
    result = services.advance(run)
    assert result["state"] == "failed"
    assert run.nodes.get(key="analyze").error_code == "UPSTREAM_FAILED"
    assert core.serialize_job(producer.job)["execution_requests"][0]["comment"] == "Reduce the ensemble."
    assert not Attempt.objects.exists()


def test_artifact_edges_use_retained_exact_attempt_hash_and_no_duplicate_admission(workflow_broker):
    context = workflow_broker
    budgeted(context)
    run = start(context)
    services.advance(run)
    producer = run.nodes.get(key="simulate")
    attempt = finish(context, producer)
    services.advance(run)
    consumer = run.nodes.get(key="analyze")
    assert consumer.execution_request_id
    assert consumer.resolved_inputs[0]["producer_attempt_id"] == str(attempt.id)
    retained = attempt.artifacts.get(path="summary.json")
    assert consumer.resolved_inputs[0]["artifact_id"] == str(retained.id)
    assert consumer.resolved_inputs[0]["sha256"] == retained.blob.sha256
    assert next(item for item in consumer.job.request["files"] if item["path"] == "input/summary.json")["sha256"] == retained.blob.sha256
    for _ in range(3):
        services.advance(run)
    assert Attempt.objects.count() == 2
    finish(context, consumer)
    assert services.advance(run)["state"] == "succeeded"


def test_missing_output_blocks_downstream_even_if_scheduler_succeeded(workflow_broker):
    context = workflow_broker
    budgeted(context)
    run = start(context)
    services.advance(run)
    finish(context, run.nodes.get(key="simulate"), output=False)
    result = services.advance(run)
    consumer = run.nodes.get(key="analyze")
    assert result["state"] == "failed"
    assert consumer.state == "blocked_artifact" and consumer.job_id is None
    assert Attempt.objects.count() == 1


def test_recovered_outputs_reopen_failed_workflow(workflow_broker, django_capture_on_commit_callbacks):
    context = workflow_broker
    budgeted(context)
    run = start(context)
    services.advance(run)
    attempt = finish(context, run.nodes.get(key="simulate"), output=False)
    Attempt.objects.filter(id=attempt.id).update(collection_state="failed", collection_failures=3)
    assert services.advance(run)["state"] == "failed"
    Attempt.objects.filter(id=attempt.id).update(collection_pending=True, next_poll_at=timezone.now())
    lease = core.lease_work(context.worker)["lease"]
    publish_output(lease, "summary.json", b"{}")
    with django_capture_on_commit_callbacks(execute=True):
        core.report(context.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
            "outcome": "observed", "native_identity": attempt.native_identity, "observation": attempt.observation,
            "collection_state": "complete"})
    run.refresh_from_db()
    assert run.state == "running"
    services.before_lease(context.worker)
    assert run.nodes.get(key="analyze").execution_request_id
    assert Attempt.objects.count() == 2


def test_unsettled_terminal_producer_never_releases_consumer(workflow_broker):
    context = workflow_broker
    budgeted(context)
    run = start(context)
    services.advance(run)
    finish(context, run.nodes.get(key="simulate"), final=False)
    result = services.advance(run)
    assert next(n for n in result["nodes"] if n["key"] == "simulate")["state"] == "awaiting_accounting"
    assert run.nodes.get(key="analyze").job_id is None and Attempt.objects.count() == 1


def test_budget_and_lifetime_caps_apply_to_each_node(workflow_broker):
    context = workflow_broker
    budgeted(context)
    context.grant.policy["max_total_jobs"] = 1
    context.grant.save(update_fields=["policy"])
    run = start(context)
    services.advance(run)
    finish(context, run.nodes.get(key="simulate"))
    result = services.advance(run)
    consumer = run.nodes.get(key="analyze")
    assert result["state"] == "waiting_budget" and consumer.error_code == "TOTAL_JOB_LIMIT"
    assert Attempt.objects.count() == 1


def test_live_key_revocation_stops_new_nodes(workflow_broker):
    context = workflow_broker
    budgeted(context)
    run = start(context)
    ProjectCredential.objects.filter(id=context.credential.id).update(revoked=True)
    with pytest.raises(BrokerError, match="active key"):
        services.advance(run)
    assert not Attempt.objects.exists()


def test_node_cannot_be_admitted_twice_through_generic_job_api(workflow_broker):
    context = workflow_broker
    budgeted(context)
    run = start(context)
    services.advance(run)
    producer = run.nodes.get(key="simulate")
    with pytest.raises(BrokerError, match="already has an execution request"):
        with transaction.atomic():
            core.request_execution(producer.job, context.credential)
    assert Attempt.objects.count() == 1


def test_cancelling_waiting_review_prevents_later_human_approval(workflow_broker):
    context = workflow_broker
    run = start(context)
    services.advance(run)
    producer = run.nodes.get(key="simulate")
    context.grant.refresh_from_db()
    assert context.grant.reserved_cpu == producer.job.manifest["reservation"]["cpu_core_seconds"]
    assert producer.execution_request.reservation_state == "held"
    services.cancel(run, context.credential)
    services.cancel(run, context.credential)
    producer.execution_request.refresh_from_db()
    assert producer.execution_request.state == "cancelled"
    assert producer.execution_request.reservation_state == "released"
    context.grant.refresh_from_db()
    assert context.grant.reserved_cpu == context.grant.reserved_gpu == 0
    assert producer.execution_request.budget_entries.filter(action="hold_release").count() == 1
    with pytest.raises(BrokerError):
        decide(context, producer.job, producer.execution_request)
    assert not Attempt.objects.exists()
    assert services.advance(run)["state"] == "cancelled"


def test_reading_run_does_not_admit_and_foreign_project_cannot_read(workflow_broker):
    context = workflow_broker
    run = start(context)
    client = Client()
    response = client.get(f"/api/v1/workflow-runs/{run.id}", HTTP_AUTHORIZATION="Bearer " + context.token)
    assert response.status_code == 200 and not Attempt.objects.exists()
    other = deepcopy(context.credential)
    other.pk = None
    other.project = Project.objects.create(owner=context.outsider, name="foreign")
    other.issued_by = context.outsider
    from compute_relay.control.auth import token_hash
    other.token_hash = token_hash("synthetic-foreign-workflow-key")
    other.save()
    assert client.get(f"/api/v1/workflow-runs/{run.id}", HTTP_AUTHORIZATION="Bearer synthetic-foreign-workflow-key").status_code == 404


def test_sealed_revision_changes_are_detected(workflow_broker):
    context = workflow_broker
    run = start(context)
    document = deepcopy(run.revision.document)
    document["name"] = "changed"
    WorkflowRevision.objects.filter(id=run.revision_id).update(document=document)
    with pytest.raises(BrokerError, match="sealed workflow"):
        services.advance(run)
    assert not Attempt.objects.exists()


def test_failed_independent_node_does_not_drop_other_pending_reviews(workflow_broker):
    context = workflow_broker
    run = start(context, graph(context, edges=False))
    services.advance(run)
    producer = run.nodes.get(key="simulate")
    decide(context, producer.job, producer.execution_request, approved=False)
    assert services.advance(run)["state"] == "awaiting_approval"


def test_prepare_and_start_api_are_idempotent(workflow_broker):
    context = workflow_broker
    document = graph(context)
    client = Client()
    headers = {"HTTP_AUTHORIZATION": "Bearer " + context.token, "HTTP_IDEMPOTENCY_KEY": "prepare-science-graph"}
    first = client.post(f"/api/v1/projects/{context.project.id}/workflow-revisions", json.dumps(document), content_type="application/json", **headers)
    second = client.post(f"/api/v1/projects/{context.project.id}/workflow-revisions", json.dumps(document), content_type="application/json", **headers)
    assert first.status_code == second.status_code == 201 and first.json() == second.json()
    headers["HTTP_IDEMPOTENCY_KEY"] = "start-science-graph"
    for _ in range(2):
        response = client.post(f"/api/v1/workflow-revisions/{first.json()['id']}/runs", "{}", content_type="application/json", **headers)
        assert response.status_code == 201
    assert WorkflowRun.objects.count() == 1 and not Attempt.objects.exists()


def test_pending_review_also_has_exactly_one_execution_request(workflow_broker):
    context = workflow_broker
    run = start(context)
    services.advance(run)
    producer = run.nodes.get(key="simulate")
    with pytest.raises(BrokerError, match="already has an execution request"):
        with transaction.atomic():
            core.request_execution(producer.job, context.credential)
    assert ExecutionRequest.objects.count() == 1 and not Attempt.objects.exists()


def test_ready_runs_rotate_past_waiting_approvals(workflow_broker):
    context = workflow_broker
    runs = [start(context) for _ in range(6)]
    services.before_lease(context.worker)
    assert ExecutionRequest.objects.count() == 4
    services.before_lease(context.worker)
    assert ExecutionRequest.objects.count() == 6
    for run in runs:
        assert run.nodes.get(key="simulate").execution_request_id


def test_expired_preparation_claim_recovers_same_job(workflow_broker, monkeypatch):
    context = workflow_broker
    budgeted(context)
    run = start(context)
    real_prepare = core.prepare_job
    monkeypatch.setattr(core, "prepare_job", lambda job: (_ for _ in ()).throw(RuntimeError("synthetic crash")))
    with pytest.raises(RuntimeError, match="synthetic crash"):
        services.advance(run)
    producer = run.nodes.get(key="simulate")
    original_job_id = producer.job_id
    assert original_job_id and producer.claim_expires_at and not Attempt.objects.exists()
    monkeypatch.setattr(core, "prepare_job", real_prepare)
    producer.claim_expires_at = timezone.now() - timedelta(seconds=1)
    producer.save(update_fields=["claim_expires_at"])
    services.advance(run)
    producer.refresh_from_db()
    assert producer.job_id == original_job_id and Attempt.objects.count() == 1


def test_cancel_during_preparation_creates_no_review_or_attempt(workflow_broker, monkeypatch):
    context = workflow_broker
    run = start(context)
    real_prepare = core.prepare_job

    def cancel_while_preparing(job):
        real_prepare(job)
        services.cancel(WorkflowRun.objects.get(id=run.id), context.credential)

    monkeypatch.setattr(core, "prepare_job", cancel_while_preparing)
    services.advance(run)
    assert not ExecutionRequest.objects.exists() and not Attempt.objects.exists()


def test_cancellation_completion_is_refreshed_by_worker_without_status_get(workflow_broker):
    context = workflow_broker
    budgeted(context)
    run = start(context)
    services.advance(run)
    producer = run.nodes.get(key="simulate")
    attempt = Attempt.objects.get(execution_request_id=producer.execution_request_id)
    attempt.dispatch_started = True
    attempt.native_identity = {"job_id": "synthetic"}
    attempt.save(update_fields=["dispatch_started", "native_identity"])
    services.cancel(run, context.credential)
    run.refresh_from_db()
    assert run.state == "cancelling"
    Attempt.objects.filter(id=attempt.id).update(state="cancelled", accounting_state="settled")
    services.before_lease(context.worker)
    run.refresh_from_db()
    assert run.state == "cancelled"


def test_producer_output_recovery_keeps_consumer_waiting(workflow_broker):
    context = workflow_broker
    budgeted(context)
    run = start(context)
    services.advance(run)
    producer = run.nodes.get(key="simulate")
    attempt = finish(context, producer, output=False)
    Attempt.objects.filter(id=attempt.id).update(collection_pending=True, collection_state="retry_pending")
    waiting = services.advance(run)
    assert next(node for node in waiting["nodes"] if node["key"] == "simulate")["state"] == "awaiting_outputs"
    consumer = run.nodes.get(key="analyze")
    assert consumer.job_id is None and consumer.state != "blocked_artifact"
    Attempt.objects.filter(id=attempt.id).update(collection_pending=False, collection_state="complete")
    from compute_relay.control.models import Artifact
    from compute_relay.control.storage import store_blob
    blob = store_blob(context.project, b'{"mean": 0.25}\n')
    Artifact.objects.create(attempt=attempt, path="summary.json", blob=blob)
    services.advance(run)
    consumer.refresh_from_db()
    assert consumer.job_id is not None


@pytest.mark.parametrize("operation", ["create", "execute"])
def test_workflow_waits_for_rate_capacity_then_resumes_the_same_node(workflow_broker, settings, monkeypatch, operation):
    context = workflow_broker
    budgeted(context)
    now = [timezone.now() + timedelta(hours=1)]
    monkeypatch.setattr(timezone, "now", lambda: now[0])
    settings.PROJECT_ADMISSION_BURST = 1
    run = start(context)
    if operation == "execute":
        now[0] += timedelta(seconds=1)
        core.request_execution(Job.objects.get(project=context.project), context.credential)
    result = services.advance(run)
    node = run.nodes.get(key="simulate")
    assert result["state"] == node.state == "waiting_capacity" and node.error_code == "RATE_LIMITED"
    assert node.execution_request_id is None
    assert (node.job_id is None) == (operation == "create")
    saved_job_id = node.job_id
    assert node.next_check_at == now[0] + timedelta(seconds=1)
    now[0] += timedelta(seconds=1)
    services.advance(run)
    node.refresh_from_db()
    assert node.execution_request_id and node.state == "authorized" and node.error_code == ""
    if saved_job_id:
        assert node.job_id == saved_job_id


def test_workflow_waits_for_backlog_capacity_until_unused_draft_is_archived(workflow_broker, settings, monkeypatch):
    from compute_relay.control.maintenance import compact_draft_jobs
    context = workflow_broker
    budgeted(context)
    now = [timezone.now() + timedelta(hours=1)]
    monkeypatch.setattr(timezone, "now", lambda: now[0])
    settings.MAX_PROJECT_OUTSTANDING_JOBS = 1
    run = start(context)
    result = services.advance(run)
    node = run.nodes.get(key="simulate")
    assert result["state"] == "waiting_capacity" and node.error_code == "BACKLOG_LIMIT"
    assert node.job_id is None and Attempt.objects.count() == 0
    assert compact_draft_jobs(owner=context.owner, before=now[0] + timedelta(seconds=1), apply=True)["archived"] == 1
    now[0] += timedelta(seconds=30)
    services.advance(run)
    node.refresh_from_db()
    assert node.execution_request_id and node.state == "authorized" and Attempt.objects.count() == 1
