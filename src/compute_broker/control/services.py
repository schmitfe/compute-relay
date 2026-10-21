"""Authoritative state transitions. All admission paths share these transactions."""

import base64
import hashlib
import uuid
from datetime import datetime, timedelta

import rfc8785
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from compute_broker.domain.validation import relative_path, validate
from .auth import human_permission, require_connection, connection_allowed
from .errors import BrokerError
from .models import (ApprovalDecision, Artifact, Attempt, AuditEvent, Blob, Connection,
                     Environment, ExecutionRequest, Grant, IdempotencyRecord, Job,
                     JobFile, LedgerEntry, Project, ProjectCredential, WorkerReport)
from .storage import read_blob, store_blob

TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
DEFAULT_MAX_TOTAL_JOBS = 1000


def digest(value):
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def audit(project, actor, action, obj, details=None):
    AuditEvent.objects.create(project=project, actor=str(actor), action=action, object_id=str(obj), details=details or {})


def idempotent(project, operation, key, payload, action):
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise BrokerError("IDEMPOTENCY_REQUIRED", "Supply an Idempotency-Key of at most 128 characters.")
    request_digest = digest(payload)
    with transaction.atomic():
        Project.objects.select_for_update().get(id=project.id)
        existing = IdempotencyRecord.objects.filter(project=project, operation=operation, key=key).first()
        if existing:
            if existing.request_sha256 != request_digest:
                raise BrokerError("IDEMPOTENCY_CONFLICT", "The key was used with a different request.", 409)
            return existing.response, existing.status
        response, status = action()
        IdempotencyRecord.objects.create(project=project, operation=operation, key=key, request_sha256=request_digest, response=response, status=status)
        return response, status


def allowance(project):
    grant = Grant.objects.get(project=project)
    limit = grant.policy["allowance"]
    job_limit = grant.policy.get("max_total_jobs", DEFAULT_MAX_TOTAL_JOBS)
    counts = Attempt.objects.filter(job__project=project).aggregate(
        admitted=Count("id"), active=Count("id", filter=~Q(state__in=TERMINAL)))
    return {
        "project_id": str(project.id), "mode": grant.policy["mode"], "paused": project.paused,
        "limit": limit, "spent": {"cpu_core_seconds": grant.spent_cpu, "gpu_device_seconds": grant.spent_gpu},
        "reserved": {"cpu_core_seconds": grant.reserved_cpu, "gpu_device_seconds": grant.reserved_gpu},
        "available": {"cpu_core_seconds": max(0, limit["cpu_core_seconds"] - grant.spent_cpu - grant.reserved_cpu), "gpu_device_seconds": max(0, limit["gpu_device_seconds"] - grant.spent_gpu - grant.reserved_gpu)},
        "debt": {"cpu_core_seconds": max(0, grant.spent_cpu + grant.reserved_cpu - limit["cpu_core_seconds"]), "gpu_device_seconds": max(0, grant.spent_gpu + grant.reserved_gpu - limit["gpu_device_seconds"])},
        "jobs": {"limit": job_limit, "admitted": counts["admitted"],
                 "remaining": max(0, job_limit - counts["admitted"]),
                 "active": counts["active"], "active_limit": grant.policy["max_active_jobs"]},
    }


def bound_environment(project, request):
    """Recipes are cluster-specific even when two interpreters share a path."""
    reference = request["environment"]
    environment = project.environments.filter(
        id=reference["id"], revision_sha256=reference["revision_sha256"],
        connection_id=request["connection_id"],
    ).first()
    if environment is None:
        raise BrokerError("REFERENCE_DENIED", "Select an environment bound to this project and cluster.", 403)
    return environment


def create_job(credential, document, previous=None):
    validate("job-request.schema.json", document)
    project = credential.project
    if document["project_id"] != str(project.id):
        raise BrokerError("NOT_FOUND", "Project not found.", 404)
    require_connection(credential, document["connection_id"])
    connection = project.connections.filter(id=document["connection_id"], enabled=True).first()
    bound_environment(project, document)
    if connection is None:
        raise BrokerError("REFERENCE_DENIED", "Connection or environment is not available to this project.", 403)
    descriptors = []
    for item in document["files"]:
        blob = Blob.objects.filter(project=project, sha256=item["sha256"], size_bytes=item["size_bytes"]).first()
        if blob is None:
            raise BrokerError("OBJECT_MISSING", "Upload and verify every referenced file first.", 404)
        descriptors.append((item, blob))
    job = Job.objects.create(project=project, credential=credential, request=document, previous=previous)
    JobFile.objects.bulk_create([JobFile(job=job, path=item["path"], blob=blob, executable=item["executable"]) for item, blob in descriptors])
    audit(project, credential.id, "job.created", job.id)
    return {"id": str(job.id), "state": job.state}, 201


def prepare_job(job):
    from compute_broker.backends import prepare, verify_package
    if job.manifest:
        return serialize_job(job), 200
    connection = job.project.connections.get(id=job.request["connection_id"], enabled=True)
    environment = bound_environment(job.project, job.request)
    config = dict(connection.configuration, backend=connection.backend, revision_sha256=connection.revision_sha256)
    env = dict(environment.configuration, id=str(environment.id), revision_sha256=environment.revision_sha256)
    result = prepare(job.request, env, config, str(job.id))
    generated = {}
    for path, text in result["generated_files"].items():
        relative_path(path)
        generated[path] = store_blob(job.project, text.encode("utf-8"))
    with transaction.atomic():
        current = Job.objects.select_for_update().get(id=job.id)
        if not current.manifest:
            descriptors = {f["path"]: f for f in result["manifest"]["files"]}
            for path, blob in generated.items():
                JobFile.objects.create(job=current, path=path, blob=blob, executable=descriptors[path]["executable"])
            files = {entry.path: read_blob(entry.blob) for entry in current.files.select_related("blob")}
            verify_package(result["manifest"], result["manifest_sha256"], files)
            validate("prepared-job.schema.json", result["manifest"])
            current.manifest = result["manifest"]
            current.manifest_sha256 = result["manifest_sha256"]
            current.state = "prepared"
            current.save(update_fields=["manifest", "manifest_sha256", "state"])
            audit(current.project, "control", "job.prepared", current.id, {"manifest_sha256": current.manifest_sha256})
    return serialize_job(Job.objects.get(id=job.id)), 200


def _within_envelope(job, policy):
    allocation = job.manifest["maximum_allocation"]
    limits = policy["per_job_limits"]
    return (allocation["cpu_cores"] <= limits["cpu_cores"] and allocation["gpu_devices"] <= limits["gpu_devices"]
            and allocation["time_limit_seconds"] <= limits["time_limit_seconds"]
            and allocation["maximum_charge_duration_seconds"] <= limits["maximum_charge_duration_seconds"]
            and job.request["resources"]["memory_mib"] <= limits["memory_mib"])


def authorize(execution_request, *, decision=None):
    """Caller transaction serializes project/grant and consumes a decision once."""
    job = execution_request.job
    project = Project.objects.select_for_update().get(id=job.project_id)
    grant = Grant.objects.select_for_update().get(project=project)
    policy = grant.policy
    validate("project-policy.schema.json", policy)
    if project.paused:
        raise BrokerError("PROJECT_PAUSED", "New execution is paused.", 409)
    if datetime.fromisoformat(policy["admission_expires_at"].replace("Z", "+00:00")) <= timezone.now():
        raise BrokerError("GRANT_EXPIRED", "Project allowance has expired.", 409)
    credential = ProjectCredential.objects.get(id=execution_request.credential_id) if execution_request.credential_id else None
    if credential and (credential.revoked or credential.expires_at <= timezone.now()):
        raise BrokerError("AUTH_REQUIRED", "The requesting credential is no longer valid.", 403)
    if credential:
        require_connection(credential, job.request["connection_id"])
        if credential.execution_mode == "approval_required" and decision is None:
            raise BrokerError("APPROVAL_REQUIRED", "This API key requires human confirmation for each execution.", 409)
    bound_environment(project, job.request)
    connection = project.connections.filter(id=job.request["connection_id"], enabled=True).first()
    if connection is None or connection.worker.revoked or connection.revision_sha256 != job.manifest["connection_revision_sha256"]:
        raise BrokerError("REVISION_CHANGED", "Connection configuration changed; prepare a new revision.", 409)
    if connection.configuration.get("site_profile", {}).get("automatic_execution_allowed") is False and decision is None:
        raise BrokerError("PROFILE_REVIEW_REQUIRED", "This cluster profile requires human confirmation for each execution.", 409)
    if str(connection.id) not in policy["allowed_connection_ids"] or job.request["environment"]["revision_sha256"] not in policy["allowed_environment_revision_sha256"]:
        raise BrokerError("POLICY_DENIED", "Connection or environment is outside project policy.", 403)
    gpu_type = job.request["resources"].get("gpu_type")
    if gpu_type and gpu_type not in policy["allowed_gpu_types"]:
        raise BrokerError("POLICY_DENIED", "GPU type is outside project policy.", 403)
    if not _within_envelope(job, policy) and not (policy["mode"] == "mixed" and decision):
        raise BrokerError("RESOURCE_LIMIT", "Effective allocation exceeds the project envelope.", 409)
    attempts = Attempt.objects.filter(job__project=project)
    if attempts.count() >= policy.get("max_total_jobs", DEFAULT_MAX_TOTAL_JOBS):
        raise BrokerError("TOTAL_JOB_LIMIT", "The project has reached its lifetime submission limit.", 409)
    if attempts.exclude(state__in=TERMINAL).count() >= policy["max_active_jobs"]:
        raise BrokerError("ACTIVE_JOB_LIMIT", "The project has reached its active job limit.", 409)
    reservation = job.manifest["reservation"]
    cpu, gpu = reservation["cpu_core_seconds"], reservation["gpu_device_seconds"]
    if grant.spent_cpu + grant.reserved_cpu + cpu > policy["allowance"]["cpu_core_seconds"] or grant.spent_gpu + grant.reserved_gpu + gpu > policy["allowance"]["gpu_device_seconds"]:
        raise BrokerError("BUDGET_UNAVAILABLE", "Insufficient unreserved project allowance.", 409)
    if decision and (not decision.approved or decision.consumed_at or decision.manifest_sha256 != job.manifest_sha256):
        raise BrokerError("APPROVAL_CONSUMED", "Approval is invalid or already consumed.", 409)
    attempt = Attempt.objects.create(execution_request=execution_request, job=job, connection=connection, grant=grant, reservation_cpu=cpu, reservation_gpu=gpu)
    grant.reserved_cpu += cpu
    grant.reserved_gpu += gpu
    grant.save(update_fields=["reserved_cpu", "reserved_gpu"])
    LedgerEntry.objects.create(grant=grant, attempt=attempt, action="reserve", cpu=cpu, gpu=gpu)
    if decision:
        decision.consumed_at = timezone.now()
        decision.save(update_fields=["consumed_at"])
    execution_request.state = "authorized"
    execution_request.save(update_fields=["state"])
    audit(project, "control", "attempt.authorized", attempt.id, {"reservation": reservation})
    return attempt


def request_execution(job, credential):
    require_connection(credential, job.request["connection_id"])
    bound_environment(job.project, job.request)
    if not job.manifest:
        raise BrokerError("NOT_PREPARED", "Prepare the job before requesting execution.", 409)
    grant = Grant.objects.select_for_update().get(project=job.project)
    execution = ExecutionRequest.objects.create(job=job, credential=credential)
    mode = grant.policy["mode"]
    attempt = None
    credential.refresh_from_db()
    connection = job.project.connections.get(id=job.request["connection_id"])
    profile_allows_automatic = connection.configuration.get("site_profile", {}).get("automatic_execution_allowed") is not False
    if profile_allows_automatic and credential.execution_mode != "approval_required" and (mode == "budgeted" or (mode == "mixed" and _within_envelope(job, grant.policy))):
        attempt = authorize(execution)
    audit(job.project, credential.id, "execution.requested", execution.id)
    return {"id": str(execution.id), "state": execution.state, "attempt_id": str(attempt.id) if attempt else None}, 202


def decide(job, user, document):
    human_permission(user, job.project, "approver")
    execution = ExecutionRequest.objects.select_for_update().filter(id=document["execution_request_id"], job=job).first()
    if execution is None:
        raise BrokerError("NOT_FOUND", "Execution request not found.", 404)
    if execution.state != "awaiting_approval" or ApprovalDecision.objects.filter(execution_request=execution).exists():
        raise BrokerError("DECISION_EXISTS", "This execution request already has a decision.", 409)
    if document["manifest_sha256"] != job.manifest_sha256:
        raise BrokerError("REVISION_CHANGED", "The reviewed package no longer matches.", 409)
    if type(document["approved"]) is not bool:
        raise BrokerError("INVALID_REQUEST", "approved must be a boolean.")
    decision = ApprovalDecision.objects.create(execution_request=execution, approver=user, approved=document["approved"], manifest_sha256=job.manifest_sha256, comment=document.get("comment", "")[:4000])
    attempt = None
    if decision.approved:
        attempt = authorize(execution, decision=decision)
    else:
        execution.state = "rejected"
        execution.save(update_fields=["state"])
    audit(job.project, user.id, "execution.approved" if decision.approved else "execution.rejected", execution.id)
    return {"id": str(execution.id), "state": execution.state, "attempt_id": str(attempt.id) if attempt else None}, 200


def serialize_attempt(attempt):
    return {"id": str(attempt.id), "state": attempt.state, "accounting_state": attempt.accounting_state, "native_identity": attempt.native_identity, "error_code": attempt.error_code}


def serialize_job(job, *, package=False):
    executions = []
    for execution in job.execution_requests.all().order_by("created_at"):
        decision = ApprovalDecision.objects.filter(execution_request=execution).first()
        executions.append({"id": str(execution.id), "state": execution.state, "comment": decision.comment if decision else None})
    result = {"id": str(job.id), "name": job.name, "project_id": str(job.project_id),
              "project_name": job.project.name, "owner": {"id": str(job.project.owner_id), "username": job.project.owner.username},
              "submitted_using": {"id": str(job.credential_id), "name": job.credential.name} if job.credential_id else None,
              "state": job.display_state, "package_state": job.state, "request": job.request,
              "manifest_sha256": job.manifest_sha256, "execution_requests": executions,
              "attempts": [serialize_attempt(a) for a in job.attempts.order_by("created_at")]}
    if package or job.manifest:
        result["manifest"] = job.manifest
    if package:
        result["files"] = [{"path": entry.path, "sha256": entry.blob.sha256, "size_bytes": entry.blob.size_bytes, "text": read_blob(entry.blob).decode("utf-8", errors="replace")} for entry in job.files.select_related("blob").order_by("path")]
    return result


def lease_work(worker):
    now = timezone.now()
    with transaction.atomic():
        candidates = Attempt.objects.select_for_update().select_related("job__project", "connection").filter(connection__worker=worker, next_poll_at__lte=now).exclude(accounting_state__in=["settled", "released"]).filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)).order_by("created_at")
        for attempt in candidates[:30]:
            if not attempt.dispatch_started and (attempt.job.project.paused or not attempt.connection.enabled):
                continue
            if attempt.cancel_requested and attempt.native_identity:
                operation = "cancel"
            elif attempt.dispatch_started and not attempt.native_identity:
                operation = "reconcile"
            elif attempt.native_identity:
                operation = "observe"
            else:
                operation = "submit"
            attempt.generation += 1
            attempt.lease_id = uuid.uuid4()
            attempt.lease_expires_at = now + timedelta(seconds=60)
            attempt.operation = operation
            attempt.save(update_fields=["generation", "lease_id", "lease_expires_at", "operation"])
            files = {item.path: base64.b64encode(read_blob(item.blob)).decode("ascii") for item in attempt.job.files.select_related("blob")}
            return {"lease": {"id": str(attempt.lease_id), "operation": operation, "attempt_id": str(attempt.id), "generation": attempt.generation, "connection_id": str(attempt.connection_id), "configuration": attempt.connection.configuration, "prepared": {"manifest": attempt.job.manifest, "manifest_sha256": attempt.job.manifest_sha256, "files": files}, "native_identity": attempt.native_identity}}
    return {"lease": None}


def checked_lease(worker, attempt_id, data):
    attempt = Attempt.objects.select_for_update().select_related("job__project", "connection", "execution_request__credential").filter(id=attempt_id, connection__worker=worker).first()
    if attempt is None:
        raise BrokerError("NOT_FOUND", "Attempt not found.", 404)
    if str(attempt.lease_id) != data.get("lease_id") or attempt.generation != data.get("generation") or not attempt.lease_expires_at or attempt.lease_expires_at <= timezone.now():
        raise BrokerError("STALE_LEASE", "Lease is stale or expired.", 409)
    return attempt


def dispatch(worker, attempt_id, data):
    with transaction.atomic():
        attempt = checked_lease(worker, attempt_id, data)
        if attempt.operation != "submit" or attempt.dispatch_started or attempt.state != "authorized" or attempt.accounting_state != "reserved":
            raise BrokerError("RECONCILIATION_REQUIRED", "Submission intent already exists; reconcile instead.", 409)
        credential = attempt.execution_request.credential
        if attempt.job.project.paused or not attempt.connection.enabled or (credential and (credential.revoked or credential.expires_at <= timezone.now())):
            raise BrokerError("DISPATCH_REVOKED", "Dispatch authority is no longer active.", 403)
        if credential and ("jobs:execute" not in credential.scopes or not connection_allowed(credential, attempt.connection_id)):
            raise BrokerError("DISPATCH_REVOKED", "The key no longer has execution access to this cluster.", 403)
        if not attempt.job.project.connections.filter(id=attempt.connection_id).exists():
            raise BrokerError("DISPATCH_REVOKED", "The project no longer has access to this cluster.", 403)
        if data.get("manifest_sha256") != attempt.job.manifest_sha256 or data.get("connection_revision_sha256") != attempt.connection.revision_sha256:
            raise BrokerError("REVISION_CHANGED", "Package or connection binding changed.", 409)
        policy = attempt.grant.policy
        validate("project-policy.schema.json", policy)
        if datetime.fromisoformat(policy["admission_expires_at"].replace("Z", "+00:00")) <= timezone.now():
            raise BrokerError("GRANT_EXPIRED", "Dispatch cutoff has passed.", 409)
        job = attempt.job
        bound_environment(job.project, job.request)
        decision = ApprovalDecision.objects.filter(execution_request=attempt.execution_request, approved=True, consumed_at__isnull=False).first()
        if (str(attempt.connection_id) not in policy["allowed_connection_ids"]
                or job.request["environment"]["revision_sha256"] not in policy["allowed_environment_revision_sha256"]
                or (job.request["resources"].get("gpu_type") and job.request["resources"]["gpu_type"] not in policy["allowed_gpu_types"])
                or (not _within_envelope(job, policy) and not (policy["mode"] == "mixed" and decision))
                or (policy["mode"] == "approval_required" and not decision)
                or (attempt.connection.configuration.get("site_profile", {}).get("automatic_execution_allowed") is False and not decision)
                or (credential and credential.execution_mode == "approval_required" and not decision)):
            raise BrokerError("DISPATCH_REVOKED", "Current project policy no longer authorizes dispatch.", 403)
        if job.manifest["connection_revision_sha256"] != attempt.connection.revision_sha256:
            raise BrokerError("REVISION_CHANGED", "Connection changed after admission.", 409)
        attempt.dispatch_started = True
        attempt.state = "dispatching"
        attempt.save(update_fields=["dispatch_started", "state"])
        return {"authorized": True}


def _settle(attempt, cpu, gpu, evidence, *, release=False):
    if attempt.accounting_state in {"settled", "released"}:
        return
    if type(cpu) is not int or type(gpu) is not int or min(cpu, gpu) < 0 or max(cpu, gpu) > 2**53 - 1:
        raise BrokerError("INVALID_ACCOUNTING", "Accounting counters are invalid.")
    grant = Grant.objects.select_for_update().get(id=attempt.grant_id)
    grant.reserved_cpu -= attempt.reservation_cpu
    grant.reserved_gpu -= attempt.reservation_gpu
    grant.spent_cpu += cpu
    grant.spent_gpu += gpu
    grant.save(update_fields=["reserved_cpu", "reserved_gpu", "spent_cpu", "spent_gpu"])
    action = "release" if release else "settle"
    LedgerEntry.objects.create(grant=grant, attempt=attempt, action=action, cpu=cpu, gpu=gpu, evidence=evidence)
    attempt.accounting_state = "released" if release else "settled"
    if cpu > attempt.reservation_cpu or gpu > attempt.reservation_gpu:
        Project.objects.filter(id=attempt.job.project_id).update(paused=True)
        attempt.error_code = "OBSERVED_OVERAGE"


def report(worker, attempt_id, data):
    payload_hash = digest(data)
    receipt = WorkerReport.objects.filter(lease_id=data.get("lease_id"), attempt_id=attempt_id, attempt__connection__worker=worker).first()
    if receipt:
        if receipt.payload_sha256 != payload_hash:
            raise BrokerError("REPORT_CONFLICT", "Lease result was already recorded differently.", 409)
        return receipt.response
    # Check scope before storing any result bytes. Artifact writes have separate quota transactions.
    with transaction.atomic():
        attempt = checked_lease(worker, attempt_id, data)
        permitted = set(attempt.job.request["outputs"])
        project = attempt.job.project
    artifacts = {}
    total = 0
    storage_failed = False
    for path, encoded in data.get("artifacts", {}).items():
        relative_path(path)
        if path not in permitted:
            raise BrokerError("ARTIFACT_DENIED", "Output was not declared in the approved request.", 403)
        content = base64.b64decode(encoded, validate=True)
        total += len(content)
        if total > 10 * 1024 * 1024:
            raise BrokerError("ARTIFACT_LIMIT", "Collected outputs exceed the report limit.", 413)
        try:
            artifacts[path] = store_blob(project, content, maximum=10 * 1024 * 1024)
        except BrokerError as exc:
            if exc.code not in {"STORAGE_QUOTA", "STORAGE_UNAVAILABLE", "OBJECT_INTEGRITY", "UPLOAD_LIMIT"}:
                raise
            storage_failed = True
        except OSError:
            storage_failed = True
    with transaction.atomic():
        attempt = checked_lease(worker, attempt_id, data)
        outcome = data.get("outcome")
        if outcome not in {"submitted", "submission_uncertain", "rejected", "observed", "cancel_requested"}:
            raise BrokerError("INVALID_REPORT", "Unknown worker result.")
        valid_outcomes = {"submit": {"submitted", "submission_uncertain", "rejected"},
                          "reconcile": {"submitted", "submission_uncertain", "rejected"},
                          "observe": {"observed"}, "cancel": {"cancel_requested"}}
        if outcome not in valid_outcomes.get(attempt.operation, set()):
            raise BrokerError("INVALID_REPORT", "Result does not match the leased operation.", 409)
        if outcome == "rejected" and (attempt.native_identity or data.get("native_identity") or not attempt.dispatch_started):
            raise BrokerError("INVALID_REPORT", "A known scheduler job cannot release funds as nonaccepted.", 409)
        if data.get("native_identity"):
            if attempt.native_identity and attempt.native_identity != data["native_identity"]:
                raise BrokerError("NATIVE_IDENTITY_CHANGED", "Attempt already has a different native identity.", 409)
            attempt.native_identity = data["native_identity"]
        if outcome == "submitted":
            if not attempt.dispatch_started or not attempt.native_identity:
                raise BrokerError("INVALID_REPORT", "Submission has no matching intent/native receipt.", 409)
            attempt.state = "queued"
        elif outcome == "submission_uncertain":
            attempt.state = "submission_uncertain"
        elif outcome == "rejected":
            attempt.state = "failed"
            _settle(attempt, 0, 0, {"reason": "definitive_nonacceptance"}, release=True)
        elif outcome == "cancel_requested":
            attempt.cancel_requested = False
            attempt.state = "cancel_requested"
        else:
            observation = data.get("observation", {})
            attempt.observation = observation
            state = {"completed": "succeeded", "succeeded": "succeeded", "failed": "failed", "cancelled": "cancelled", "queued": "queued", "running": "running"}.get(observation.get("state"))
            if state:
                attempt.state = state
            accounting = observation.get("accounting", {})
            if state in TERMINAL and observation.get("terminal") is True and accounting.get("final") is True:
                _settle(attempt, accounting["cpu_core_seconds"], accounting["gpu_device_seconds"], observation)
            elif state in TERMINAL:
                attempt.accounting_state = "settlement_pending"
        for field in ("stdout", "stderr"):
            if field in data:
                value = data[field]
                if not isinstance(value, str) or len(value.encode("utf-8")) > 65536:
                    raise BrokerError("LOG_LIMIT", "Logs exceed the bounded report limit.", 413)
                setattr(attempt, field, value)
        for path, blob in artifacts.items():
            existing, created = Artifact.objects.get_or_create(attempt=attempt, path=path, defaults={"blob": blob})
            if not created and existing.blob_id != blob.id:
                raise BrokerError("ARTIFACT_CHANGED", "Final artifact bytes changed.", 409)
        if attempt.error_code != "OBSERVED_OVERAGE":
            attempt.error_code = data.get("error_code", attempt.error_code)[:80]
        if storage_failed and attempt.error_code != "OBSERVED_OVERAGE":
            attempt.error_code = "OUTPUT_STORAGE_UNAVAILABLE"
        attempt.lease_expires_at = None
        delay = 10 if attempt.state == "submission_uncertain" or attempt.accounting_state == "settlement_pending" else 2
        attempt.next_poll_at = timezone.now() + timedelta(seconds=delay)
        attempt.save()
        response = {"accepted": True, "attempt": serialize_attempt(attempt)}
        WorkerReport.objects.create(lease_id=data["lease_id"], attempt=attempt, generation=attempt.generation, payload_sha256=payload_hash, response=response)
        return response


def cancel_attempt(attempt):
    with transaction.atomic():
        attempt = Attempt.objects.select_for_update().get(id=attempt.id)
        if attempt.state not in TERMINAL:
            if not attempt.dispatch_started:
                attempt.state = "cancelled"
                _settle(attempt, 0, 0, {"reason": "cancelled_before_dispatch"}, release=True)
                attempt.lease_id = None
                attempt.lease_expires_at = None
                attempt.generation += 1
            else:
                attempt.cancel_requested = True
            attempt.next_poll_at = timezone.now()
            attempt.save()
        return serialize_attempt(attempt), 202
