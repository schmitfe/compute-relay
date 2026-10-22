"""Workflow orchestration delegates each concrete execution to the core ledger."""

from copy import deepcopy
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from jsonschema.exceptions import ValidationError

from compute_broker.control import services as core
from compute_broker.control.auth import require_connection
from compute_broker.control.errors import BrokerError
from compute_broker.control.models import Artifact, Attempt, Blob, Job, Project
from .models import WorkflowNodeRun, WorkflowRevision, WorkflowRun
from .validation import validate_workflow


def check_credential(credential, *, execute=False):
    required = {"workflows:write", "jobs:write"}
    if execute:
        required.add("jobs:execute")
    if credential.revoked or credential.expires_at <= timezone.now() or not required <= set(credential.scopes):
        raise BrokerError("WORKFLOW_SCOPE_DENIED", "An active key with workflow and job scopes is required.", 403)


def authorize_nodes(credential, document):
    by_key, dependencies, ordered = validate_workflow(document)
    for node in by_key.values():
        request = node["request"]
        if request["project_id"] != str(credential.project_id):
            raise BrokerError("REFERENCE_DENIED", "Every workflow node must belong to this project.", 403)
        require_connection(credential, request["connection_id"])
        core.bound_environment(credential.project, request)
        for file in request["files"]:
            if not Blob.objects.filter(project=credential.project, sha256=file["sha256"], size_bytes=file["size_bytes"]).exists():
                raise BrokerError("OBJECT_MISSING", "Upload every node's code/input files first.", 404)
    if any(node.get("inputs") for node in by_key.values()) and "artifacts:read" not in credential.scopes:
        raise BrokerError("SCOPE_DENIED", "Artifact-dependent workflows require artifacts:read.", 403)
    return by_key, dependencies, ordered


def create_revision(credential, document):
    check_credential(credential)
    authorize_nodes(credential, document)
    revision = WorkflowRevision.objects.create(project=credential.project, name=document["name"],
        document=deepcopy(document), sha256=core.digest(document))
    revision.connections.set({node["request"]["connection_id"] for node in document["nodes"]})
    core.audit(credential.project, credential.id, "workflow.prepared", revision.id, {"sha256": revision.sha256})
    return {"id": str(revision.id), "sha256": revision.sha256, "state": "prepared", "node_count": len(document["nodes"])}, 201


def start_run(credential, revision):
    check_credential(credential, execute=True)
    check_revision(revision, credential)
    authorize_nodes(credential, revision.document)
    run = WorkflowRun.objects.create(revision=revision, credential=credential)
    WorkflowNodeRun.objects.bulk_create([WorkflowNodeRun(run=run, key=node["key"]) for node in revision.document["nodes"]])
    core.audit(credential.project, credential.id, "workflow.started", run.id)
    return serialize_run(run), 201


def check_revision(revision, credential):
    if revision.project_id != credential.project_id or core.digest(revision.document) != revision.sha256:
        raise BrokerError("REVISION_CHANGED", "The sealed workflow revision changed.", 409)


def _attempt(node):
    return Attempt.objects.filter(execution_request_id=node.execution_request_id).first() if node.execution_request_id else None


def _refresh(run):
    nodes = list(run.nodes.select_related("job", "execution_request"))
    for node in nodes:
        if node.execution_request_id:
            attempt = _attempt(node)
            if attempt:
                node.state = attempt.state
                if attempt.state in core.TERMINAL and attempt.accounting_state not in {"settled", "released"}:
                    node.state = "awaiting_accounting"
            else:
                node.state = node.execution_request.state
            node.save(update_fields=["state"])
    states = {node.state for node in nodes}
    finished = states <= {"succeeded", "failed", "cancelled", "rejected", "expired", "blocked_upstream", "blocked_artifact"}
    if run.cancel_requested:
        run.state = "cancelled" if finished else "cancelling"
    elif states == {"succeeded"}:
        run.state = "succeeded"
    elif finished:
        run.state = "failed"
    elif "awaiting_approval" in states:
        run.state = "awaiting_approval"
    elif "waiting_budget" in states:
        run.state = "waiting_budget"
    else:
        run.state = "running"
    run.save(update_fields=["state"])
    return {node.key: node for node in nodes}


def _resolve(run, node, specification, dependencies, records, credential):
    predecessors = [records[key] for key in dependencies[node.key]]
    if any(item.state in {"failed", "cancelled", "rejected", "expired", "blocked_upstream", "blocked_artifact"} for item in predecessors):
        node.state = "blocked_upstream"
        node.error_code = "UPSTREAM_FAILED"
        node.save(update_fields=["state", "error_code"])
        return None
    if any(item.state != "succeeded" for item in predecessors):
        return None
    request = deepcopy(specification["request"])
    resolved = []
    for edge in specification.get("inputs", []):
        producer = records[edge["node"]]
        require_connection(credential, producer.job.request["connection_id"])
        artifact = Artifact.objects.select_related("blob", "attempt").filter(
            attempt__execution_request_id=producer.execution_request_id,
            attempt__job__project=credential.project, path=edge["output"]).first()
        if artifact is None:
            node.state, node.error_code = "blocked_artifact", "UPSTREAM_OUTPUT_MISSING"
            node.save(update_fields=["state", "error_code"])
            return None
        request["files"].append({"path": edge["path"], "sha256": artifact.blob.sha256,
            "size_bytes": artifact.blob.size_bytes, "executable": False})
        resolved.append({"artifact_id": str(artifact.id), "sha256": artifact.blob.sha256,
            "producer_attempt_id": str(artifact.attempt_id), "path": edge["path"]})
    return request, resolved


def advance(run, credential=None, *, maximum=2):
    """Idempotently materialize at most two nodes outside blob transactions."""
    if credential is not None and credential.id != run.credential_id:
        raise BrokerError("WORKFLOW_KEY_CHANGED", "Resume with the run's originating key or create a new run.", 403)
    credential = run.credential
    credential.refresh_from_db()
    run.refresh_from_db()
    run.revision.refresh_from_db()
    check_credential(credential, execute=True)
    check_revision(run.revision, credential)
    specifications, dependencies, order = authorize_nodes(credential, run.revision.document)
    records = _refresh(run)
    if run.cancel_requested:
        return serialize_run(run)
    processed = 0
    for key in order:
        node = records[key]
        if processed >= maximum or node.execution_request_id or node.state in {"blocked_upstream", "blocked_artifact"} or node.next_check_at > timezone.now():
            continue
        resolved = _resolve(run, node, specifications[key], dependencies, records, credential)
        if resolved is None:
            continue
        request, inputs = resolved
        with transaction.atomic():
            Project.objects.select_for_update().get(id=credential.project_id)
            current = WorkflowNodeRun.objects.select_for_update().get(id=node.id)
            if current.execution_request_id or (current.claim_expires_at and current.claim_expires_at > timezone.now()):
                continue
            if not current.job_id:
                created, _ = core.create_job(credential, request)
                job = Job.objects.get(id=created["id"])
                job.extension_name = "workflows"
                job.save(update_fields=["extension_name"])
                current.job = job
                current.resolved_inputs = inputs
            current.state = "preparing"
            current.claim_expires_at = timezone.now() + timedelta(seconds=120)
            current.save()
        processed += 1
        try:
            # Generated blob reservations must commit independently of metadata.
            core.prepare_job(current.job)
            with transaction.atomic():
                Project.objects.select_for_update().get(id=credential.project_id)
                locked = WorkflowNodeRun.objects.select_for_update(of=("self",)).select_related("job").get(id=current.id)
                if WorkflowRun.objects.get(id=run.id).cancel_requested:
                    locked.state, locked.claim_expires_at = "cancelled", None
                    locked.save(update_fields=["state", "claim_expires_at"])
                    continue
                if not locked.execution_request_id:
                    result, _ = core.request_execution(locked.job, credential)
                    locked.execution_request_id = result["id"]
                    locked.state = result["state"]
                locked.claim_expires_at = None
                locked.error_code = ""
                locked.save()
        except BrokerError as exc:
            WorkflowNodeRun.objects.filter(id=current.id).update(state="waiting_budget" if exc.code in {
                "BUDGET_UNAVAILABLE", "ACTIVE_JOB_LIMIT", "TOTAL_JOB_LIMIT"} else "blocked_policy",
                error_code=exc.code, claim_expires_at=None, next_check_at=timezone.now() + timedelta(seconds=30))
        except (ValueError, ValidationError):
            WorkflowNodeRun.objects.filter(id=current.id).update(state="blocked_policy", error_code="INVALID_NODE_REQUEST",
                claim_expires_at=None, next_check_at=timezone.now() + timedelta(seconds=30))
    _refresh(run)
    return serialize_run(run)


def validate_admission(job, credential):
    check_credential(credential, execute=True)
    node = WorkflowNodeRun.objects.select_related("run").get(job=job)
    if node.run.cancel_requested or job.attempts.exists():
        raise BrokerError("WORKFLOW_ADMISSION_DENIED", "This workflow node is cancelled or already admitted.", 409)


def validate_execution_request(job, credential):
    check_credential(credential, execute=True)
    node = WorkflowNodeRun.objects.select_related("run").get(job=job)
    if node.run.cancel_requested or node.execution_request_id or credential.id != node.run.credential_id:
        raise BrokerError("WORKFLOW_ADMISSION_DENIED", "This node already has an execution request or is no longer dispatchable.", 409)


def after_admission(attempt):
    node = WorkflowNodeRun.objects.select_for_update().get(job=attempt.job)
    if node.execution_request_id and node.execution_request_id != attempt.execution_request_id:
        raise BrokerError("WORKFLOW_ADMISSION_DENIED", "The decision does not belong to this workflow execution.", 409)
    node.execution_request = attempt.execution_request
    node.save(update_fields=["execution_request"])


def validate_dispatch(job, credential, attempt=None):
    check_credential(credential, execute=True)
    node = WorkflowNodeRun.objects.select_related("run").get(job=job)
    if node.run.cancel_requested or (attempt and node.execution_request_id != attempt.execution_request_id):
        raise BrokerError("WORKFLOW_ADMISSION_DENIED", "This workflow execution is no longer dispatchable.", 409)


def before_lease(worker):
    runs = WorkflowRun.objects.filter(revision__connections__worker=worker, next_check_at__lte=timezone.now()).exclude(
        state__in=["succeeded", "failed", "cancelled"]).distinct().order_by("next_check_at", "created_at")[:4]
    for run in runs:
        WorkflowRun.objects.filter(id=run.id).update(next_check_at=timezone.now() + timedelta(seconds=5))
        try:
            if run.cancel_requested:
                _refresh(run)
            else:
                advance(run)
        except BrokerError:
            continue  # Revoked/stale authority never stops core observations.


def cancel(run, credential):
    check_credential(credential, execute=True)
    if "jobs:cancel" not in credential.scopes:
        raise BrokerError("SCOPE_DENIED", "Workflow cancellation requires jobs:cancel.", 403)
    with transaction.atomic():
        Project.objects.select_for_update().get(id=credential.project_id)
        run.cancel_requested = True
        run.save(update_fields=["cancel_requested"])
        for node in run.nodes.select_related("job"):
            if node.job_id:
                require_connection(credential, node.job.request["connection_id"])
            attempt = _attempt(node)
            if attempt:
                core.cancel_attempt(attempt)
            else:
                node.state = "cancelled"
                if node.execution_request_id and node.execution_request.state == "awaiting_approval":
                    node.execution_request.state = "cancelled"
                    node.execution_request.save(update_fields=["state"])
                node.save(update_fields=["state"])
    _refresh(run)
    return serialize_run(run), 202


def serialize_run(run):
    return {"id": str(run.id), "revision_id": str(run.revision_id), "name": run.revision.name,
        "project_id": str(run.revision.project_id), "state": run.state, "cancel_requested": run.cancel_requested,
        "nodes": [{"key": node.key, "state": node.state, "job_id": str(node.job_id) if node.job_id else None,
            "execution_request_id": str(node.execution_request_id) if node.execution_request_id else None,
            "error_code": node.error_code, "inputs": node.resolved_inputs} for node in run.nodes.order_by("key")]}
