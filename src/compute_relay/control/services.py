"""Authoritative state transitions. All admission paths share these transactions."""

import base64
import hashlib
import logging
import json
import uuid
from datetime import datetime, timedelta

import rfc8785
from django.db import transaction
from django.db.models import Case, Count, IntegerField, Q, Sum, Value, When
from django.utils import timezone

from compute_relay.domain.validation import relative_path, validate
from compute_relay.domain.lifecycle import terminal_observation, validate_report_outcome
from .auth import human_permission, require_connection, connection_allowed, require_credential_actor
from .errors import BrokerError
from .models import (ApprovalDecision, Artifact, Attempt, AuditEvent, Blob, Connection,
                     Environment, ExecutionRequest, Grant, IdempotencyRecord, Job,
                     JobFile, LedgerEntry, Project, ProjectCredential, WorkerReport, User,
                     WorkerIdentity)
from .storage import read_blob, store_blob
from compute_relay.transfer import FileReference, INLINE_BYTES

TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
DEFAULT_MAX_TOTAL_JOBS = 1000
LEASE_SECONDS = 120
# Observe + metadata finalization + collection may each use the protected
# 600-second helper maximum. HTTP/journal overhead fits in the remaining margin.
MAX_LEASE_LIFETIME_SECONDS = 40 * 60
MAX_REPORT_RECEIPTS_PER_ATTEMPT = 16


def digest(value):
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def audit(project, actor, action, obj, details=None):
    from .metadata import append_audit
    append_audit(project, actor, action, obj, details or {})


def idempotent(project, operation, key, payload, action):
    from .metadata import idempotency_charge_id, json_size, reserve_metadata
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise BrokerError("IDEMPOTENCY_REQUIRED", "Supply an Idempotency-Key of at most 128 characters.")
    request_digest = digest(payload)
    with transaction.atomic():
        Project.objects.select_for_update().get(id=project.id)
        existing = IdempotencyRecord.objects.filter(project=project, operation=operation, key=key).first()
        if existing:
            if existing.request_sha256 != request_digest:
                raise BrokerError("IDEMPOTENCY_CONFLICT", "The key was used with a different request.", 409)
            if existing.compacted:
                return {"error": {"code": "IDEMPOTENCY_RETIRED", "message": "This request was already processed; its response was retired."}}, 409
            return existing.response, existing.status
        charge_id = idempotency_charge_id(operation, key)
        reserve_metadata(project, "idempotency", charge_id, 512)
        response, status = action()
        reserve_metadata(project, "idempotency", charge_id, 512 + json_size(response))
        IdempotencyRecord.objects.create(project=project, operation=operation, key=key, request_sha256=request_digest, response=response, status=status)
        return response, status


def allowance(project):
    grant = Grant.objects.get(project=project)
    limit = grant.policy["allowance"]
    job_limit = grant.policy.get("max_total_jobs", DEFAULT_MAX_TOTAL_JOBS)
    counts = Attempt.objects.filter(job__project=project).aggregate(
        admitted=Count("id"), active=Count("id", filter=~Q(state__in=TERMINAL)))
    pending = ExecutionRequest.objects.filter(job__project=project, reservation_state="held").aggregate(
        count=Count("id"), cpu=Sum("reservation_cpu"), gpu=Sum("reservation_gpu"))
    return {
        "project_id": str(project.id), "mode": grant.policy["mode"], "paused": project.paused,
        "per_job_limits": {field: grant.policy["per_job_limits"][field]
                           for field in ("cpu_cores", "gpu_devices", "memory_mib", "time_limit_seconds")},
        "policy_revision": grant.policy["revision"], "admission_expires_at": grant.policy["admission_expires_at"],
        "limit": limit, "spent": {"cpu_core_seconds": grant.spent_cpu, "gpu_device_seconds": grant.spent_gpu},
        "reserved": {"cpu_core_seconds": grant.reserved_cpu, "gpu_device_seconds": grant.reserved_gpu},
        "reserved_awaiting_approval": {"cpu_core_seconds": pending["cpu"] or 0,
                                       "gpu_device_seconds": pending["gpu"] or 0},
        "available": {"cpu_core_seconds": max(0, limit["cpu_core_seconds"] - grant.spent_cpu - grant.reserved_cpu), "gpu_device_seconds": max(0, limit["gpu_device_seconds"] - grant.spent_gpu - grant.reserved_gpu)},
        "debt": {"cpu_core_seconds": max(0, grant.spent_cpu + grant.reserved_cpu - limit["cpu_core_seconds"]), "gpu_device_seconds": max(0, grant.spent_gpu + grant.reserved_gpu - limit["gpu_device_seconds"])},
        "jobs": {"limit": job_limit, "admitted": counts["admitted"],
                 "remaining": max(0, job_limit - counts["admitted"]),
                 "active": counts["active"], "active_limit": grant.policy["max_active_jobs"],
                 "awaiting_approval": pending["count"]},
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


@transaction.atomic
def create_job(credential, document, previous=None, *, input_sources=None, trusted_files=None):
    from .input_sources import authorize_source, current_credential, resolve_input_blob
    from .metadata import json_size, reserve_metadata
    credential = current_credential(credential)
    validate("job-request.schema.json", document)
    project = credential.project
    Project.objects.select_for_update().get(id=project.id)
    if previous is not None:
        previous = Job.objects.filter(id=previous.id, project=project).first()
        if previous is None:
            raise BrokerError("NOT_FOUND", "Previous job not found.", 404)
        if previous.state == "archived":
            raise BrokerError("JOB_ARCHIVED", "Create a fresh job from explicit inputs.", 409)
    if document["project_id"] != str(project.id):
        raise BrokerError("NOT_FOUND", "Project not found.", 404)
    require_connection(credential, document["connection_id"])
    connection = project.connections.filter(id=document["connection_id"], enabled=True).first()
    bound_environment(project, document)
    if connection is None:
        raise BrokerError("REFERENCE_DENIED", "Connection or environment is not available to this project.", 403)
    descriptors = []
    for item in document["files"]:
        artifact = None
        if trusted_files is not None and item["path"] in trusted_files:
            blob, source_kind = trusted_files[item["path"]], "generated"
        elif input_sources is not None and item["path"] in input_sources:
            blob, artifact = input_sources[item["path"]]
            authorize_source(credential, blob, artifact)
            source_kind = "artifact" if artifact is not None else "upload"
        else:
            blob, artifact = resolve_input_blob(credential, item)
            source_kind = "artifact" if artifact is not None else "upload"
        if blob.project_id != project.id or blob.sha256 != item["sha256"] or blob.size_bytes != item["size_bytes"]:
            raise BrokerError("INPUT_SOURCE_CHANGED", "The sealed input source changed.", 409)
        descriptors.append((item, blob, source_kind, artifact))
    identifier = uuid.uuid4()
    # Reserve the duplicate manifest/request plus bounded generated descriptors
    # and row overhead before writing a draft. Duplicate blobs do not evade this.
    reserve_metadata(project, "job", identifier, 2 * json_size(document) + 32768 + 512 * len(descriptors))
    job = Job.objects.create(id=identifier, project=project, credential=credential, request=document, previous=previous)
    JobFile.objects.bulk_create([JobFile(job=job, path=item["path"], blob=blob, executable=item["executable"],
        source_kind=kind, source_artifact=artifact) for item, blob, kind, artifact in descriptors])
    audit(project, credential.id, "job.created", job.id)
    return {"id": str(job.id), "state": job.state}, 201


def prepare_job(job):
    from compute_relay.backends import prepare, verify_package
    if job.state == "archived":
        raise BrokerError("JOB_ARCHIVED", "This job was archived.", 409)
    if job.manifest:
        return serialize_job(job), 200
    connection = job.project.connections.get(id=job.request["connection_id"], enabled=True, setup_complete=True)
    environment = bound_environment(job.project, job.request)
    config = dict(connection.configuration, backend=connection.backend, revision_sha256=connection.revision_sha256)
    env = dict(environment.configuration, id=str(environment.id), revision_sha256=environment.revision_sha256)
    from .resource_api import planned_allocation, resource_error, violations_for_connection
    violations = violations_for_connection(job.request["resources"], planned_allocation(job.request["resources"], config), config)
    if violations:
        raise resource_error(violations)
    result = prepare(job.request, env, config, str(job.id))
    if job.extension_name:
        from compute_relay.extensions import services_for
        from compute_relay.backends.preparation import canonical_json_bytes
        services_for(job.extension_name)
        result["manifest"]["extension_name"] = job.extension_name
        result["manifest_sha256"] = hashlib.sha256(canonical_json_bytes(result["manifest"])).hexdigest()
    generated = {}
    for path, text in result["generated_files"].items():
        relative_path(path)
        generated[path] = store_blob(job.project, text.encode("utf-8"))
    with transaction.atomic():
        current = Job.objects.select_for_update().get(id=job.id)
        if not current.manifest:
            descriptors = {f["path"]: f for f in result["manifest"]["files"]}
            for path, blob in generated.items():
                JobFile.objects.create(job=current, path=path, blob=blob, executable=descriptors[path]["executable"], source_kind="generated")
            files = {entry.path: FileReference(entry.blob.sha256, entry.blob.size_bytes)
                if entry.blob.size_bytes > INLINE_BYTES else read_blob(entry.blob)
                for entry in current.files.select_related("blob")}
            verify_package(result["manifest"], result["manifest_sha256"], files)
            validate("prepared-job.schema.json", result["manifest"])
            current.manifest = result["manifest"]
            current.manifest_sha256 = result["manifest_sha256"]
            current.state = "prepared"
            current.save(update_fields=["manifest", "manifest_sha256", "state"])
            audit(current.project, "control", "job.prepared", current.id, {"manifest_sha256": current.manifest_sha256})
    return serialize_job(Job.objects.get(id=job.id)), 200


def _within_envelope(job, policy):
    from compute_relay.domain.resource_limits import allocation_violations
    return not allocation_violations(job.request["resources"], job.manifest["maximum_allocation"], policy["per_job_limits"])


def execution_approval(job, credential, policy=None, connection=None):
    from .resource_api import job_approval_requirements
    return job_approval_requirements(job, credential=credential, policy=policy, connection=connection)


def execution_reservation(execution):
    return {"state": execution.reservation_state, "cpu_core_seconds": execution.reservation_cpu,
            "gpu_device_seconds": execution.reservation_gpu}


def _pending_reserve(execution, grant):
    """Hold review funds before exposing the request; caller holds project/grant."""
    project = execution.job.project
    if project.paused:
        raise BrokerError("PROJECT_PAUSED", "New execution is paused.", 409)
    policy = grant.policy
    validate("project-policy.schema.json", policy)
    if datetime.fromisoformat(policy["admission_expires_at"].replace("Z", "+00:00")) <= timezone.now():
        raise BrokerError("GRANT_EXPIRED", "Project allowance has expired.", 409)
    reservation = execution.job.manifest["reservation"]
    cpu, gpu = reservation["cpu_core_seconds"], reservation["gpu_device_seconds"]
    _require_budget(grant, cpu, gpu)
    execution.reservation_cpu, execution.reservation_gpu = cpu, gpu
    execution.reservation_state = "held"
    execution.save(update_fields=["reservation_cpu", "reservation_gpu", "reservation_state"])
    grant.reserved_cpu += cpu
    grant.reserved_gpu += gpu
    grant.save(update_fields=["reserved_cpu", "reserved_gpu"])
    LedgerEntry.objects.create(grant=grant, execution_request=execution, action="hold", cpu=cpu, gpu=gpu)


def _require_budget(grant, additional_cpu=0, additional_gpu=0):
    limit = grant.policy["allowance"]
    if (grant.spent_cpu + grant.reserved_cpu + additional_cpu > limit["cpu_core_seconds"]
            or grant.spent_gpu + grant.reserved_gpu + additional_gpu > limit["gpu_device_seconds"]):
        raise BrokerError("BUDGET_UNAVAILABLE", "Insufficient unreserved project allowance. Waiting approvals and queued or running executions already hold their budget.", 409)


def _release_pending_reservation(execution, grant, reason):
    if execution.reservation_state != "held":
        return
    grant.reserved_cpu -= execution.reservation_cpu
    grant.reserved_gpu -= execution.reservation_gpu
    grant.save(update_fields=["reserved_cpu", "reserved_gpu"])
    execution.reservation_state = "released"
    execution.save(update_fields=["reservation_state"])
    LedgerEntry.objects.create(grant=grant, execution_request=execution, action="hold_release", cpu=0, gpu=0,
        evidence={"reason": reason})


def _require_executable_job(job):
    if job.state == "archived":
        raise BrokerError("JOB_ARCHIVED", "Archived jobs cannot execute. Create a new job.", 409)


def authorize(execution_request, *, decision=None):
    """Caller transaction serializes project/grant and consumes a decision once."""
    job = execution_request.job
    _require_executable_job(job)
    project = Project.objects.select_for_update().get(id=job.project_id)
    job.refresh_from_db()
    _require_executable_job(job)
    grant = Grant.objects.select_for_update().get(project=project)
    policy = grant.policy
    validate("project-policy.schema.json", policy)
    if policy["mode"] == "approval_required" and decision is None:
        raise BrokerError("APPROVAL_REQUIRED", "Project policy requires human confirmation for each execution.", 409)
    if project.paused:
        raise BrokerError("PROJECT_PAUSED", "New execution is paused.", 409)
    if datetime.fromisoformat(policy["admission_expires_at"].replace("Z", "+00:00")) <= timezone.now():
        raise BrokerError("GRANT_EXPIRED", "Project allowance has expired.", 409)
    credential = ProjectCredential.objects.get(id=execution_request.credential_id) if execution_request.credential_id else None
    if credential is None:
        raise BrokerError("HPC_IDENTITY_REQUIRED", "Execution needs a key bound to its requesting user.", 403)
    if credential.revoked or credential.expires_at <= timezone.now():
        raise BrokerError("AUTH_REQUIRED", "The requesting credential is no longer valid.", 403)
    require_credential_actor(credential)
    require_connection(credential, job.request["connection_id"])
    from .input_sources import authorize_job_inputs
    authorize_job_inputs(job, credential)
    if credential.execution_mode == "approval_required" and decision is None:
        raise BrokerError("APPROVAL_REQUIRED", "This API key requires human confirmation for each execution.", 409)
    bound_environment(project, job.request)
    connection = project.connections.filter(id=job.request["connection_id"], enabled=True, setup_complete=True).first()
    if connection is None or connection.worker.revoked or connection.revision_sha256 != job.manifest["connection_revision_sha256"]:
        raise BrokerError("REVISION_CHANGED", "Connection configuration changed; prepare a new revision.", 409)
    if connection.configuration.get("site_profile", {}).get("automatic_execution_allowed") is not True and decision is None:
        raise BrokerError("PROFILE_REVIEW_REQUIRED", "This cluster profile requires human confirmation for each execution.", 409)
    if str(connection.id) not in policy["allowed_connection_ids"] or job.request["environment"]["revision_sha256"] not in policy["allowed_environment_revision_sha256"]:
        raise BrokerError("POLICY_DENIED", "Connection or environment is outside project policy.", 403)
    gpu_type = job.request["resources"].get("gpu_type")
    if gpu_type and gpu_type not in policy["allowed_gpu_types"]:
        raise BrokerError("POLICY_DENIED", "GPU type is outside project policy.", 403)
    if not _within_envelope(job, policy) and not (policy["mode"] == "mixed" and decision):
        from .resource_api import resource_error
        from compute_relay.domain.resource_limits import allocation_violations
        raise resource_error(allocation_violations(job.request["resources"], job.manifest["maximum_allocation"], policy["per_job_limits"]))
    attempts = Attempt.objects.filter(job__project=project)
    if attempts.count() >= policy.get("max_total_jobs", DEFAULT_MAX_TOTAL_JOBS):
        raise BrokerError("TOTAL_JOB_LIMIT", "The project has reached its lifetime submission limit.", 409)
    if attempts.exclude(state__in=TERMINAL).count() >= policy["max_active_jobs"]:
        raise BrokerError("ACTIVE_JOB_LIMIT", "The project has reached its active job limit.", 409)
    reservation = job.manifest["reservation"]
    cpu, gpu = reservation["cpu_core_seconds"], reservation["gpu_device_seconds"]
    held = execution_request.reservation_state == "held"
    if held and (execution_request.reservation_cpu, execution_request.reservation_gpu) != (cpu, gpu):
        raise BrokerError("RESERVATION_CHANGED", "The pending reservation does not match this package.", 409)
    _require_budget(grant, 0 if held else cpu, 0 if held else gpu)
    if decision and (not decision.approved or decision.consumed_at or decision.manifest_sha256 != job.manifest_sha256):
        raise BrokerError("APPROVAL_CONSUMED", "Approval is invalid or already consumed.", 409)
    from compute_relay.extensions import job_hook
    job_hook(job, "validate_admission", job, credential)
    attempt = Attempt.objects.create(execution_request=execution_request, job=job, connection=connection, grant=grant, reservation_cpu=cpu, reservation_gpu=gpu)
    job_hook(job, "after_admission", attempt)
    if held:
        execution_request.reservation_state = "converted"
        LedgerEntry.objects.create(grant=grant, execution_request=execution_request,
            action="hold_convert", cpu=0, gpu=0, evidence={"attempt_id": str(attempt.id)})
    else:
        grant.reserved_cpu += cpu
        grant.reserved_gpu += gpu
        grant.save(update_fields=["reserved_cpu", "reserved_gpu"])
    LedgerEntry.objects.create(grant=grant, attempt=attempt, action="reserve", cpu=cpu, gpu=gpu)
    if decision:
        decision.consumed_at = timezone.now()
        decision.save(update_fields=["consumed_at"])
    execution_request.state = "authorized"
    execution_request.save(update_fields=["state", "reservation_state"])
    audit(project, "control", "attempt.authorized", attempt.id, {"reservation": reservation})
    return attempt


@transaction.atomic
def request_execution(job, credential):
    _require_executable_job(job)
    project = Project.objects.select_for_update().get(id=job.project_id)
    job.refresh_from_db()
    _require_executable_job(job)
    job.project = project
    credential.refresh_from_db()
    if credential.revoked or credential.expires_at <= timezone.now():
        raise BrokerError("AUTH_REQUIRED", "The requesting credential is no longer valid.", 403)
    require_credential_actor(credential)
    require_connection(credential, job.request["connection_id"])
    from .input_sources import authorize_job_inputs
    authorize_job_inputs(job, credential)
    bound_environment(job.project, job.request)
    from compute_relay.extensions import job_hook
    job_hook(job, "validate_execution_request", job, credential)
    if not job.manifest:
        raise BrokerError("NOT_PREPARED", "Prepare the job before requesting execution.", 409)
    grant = Grant.objects.select_for_update().get(project=job.project)
    connection = job.project.connections.get(id=job.request["connection_id"])
    requirements = execution_approval(job, credential, grant.policy, connection)
    execution = ExecutionRequest.objects.create(job=job, credential=credential, approval_requirements=requirements)
    attempt = None
    if not requirements["requires_human_approval"]:
        attempt = authorize(execution)
    else:
        _pending_reserve(execution, grant)
    audit(job.project, credential.id, "execution.requested", execution.id)
    return {"id": str(execution.id), "state": execution.state, "attempt_id": str(attempt.id) if attempt else None,
            "approval": requirements, "budget_reservation": execution_reservation(execution)}, 202


@transaction.atomic
def decide(job, user, document):
    human_permission(user, job.project, "approver")
    Project.objects.select_for_update().get(id=job.project_id)
    grant = Grant.objects.select_for_update().get(project_id=job.project_id)
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
        _release_pending_reservation(execution, grant, "rejected")
        execution.state = "rejected"
        execution.save(update_fields=["state"])
    audit(job.project, user.id, "execution.approved" if decision.approved else "execution.rejected", execution.id)
    return {"id": str(execution.id), "state": execution.state, "attempt_id": str(attempt.id) if attempt else None}, 200


def cancel_execution_request(execution):
    """Cancel a review hold or delegate an admitted execution to safe cancellation."""
    with transaction.atomic():
        Project.objects.select_for_update().get(id=execution.job.project_id)
        grant = Grant.objects.select_for_update().get(project_id=execution.job.project_id)
        execution = ExecutionRequest.objects.select_for_update().get(id=execution.id)
        attempt = Attempt.objects.filter(execution_request=execution).first()
        if attempt:
            result, _ = cancel_attempt(attempt)
        else:
            result = None
            if execution.state == "awaiting_approval":
                _release_pending_reservation(execution, grant, "cancelled_before_approval")
                execution.state = "cancelled"
                execution.save(update_fields=["state"])
                audit(execution.job.project, "control", "execution.cancelled", execution.id)
        return {"id": str(execution.id), "state": execution.state, "attempt_id": str(attempt.id) if attempt else None,
                "attempt": result, "budget_reservation": execution_reservation(execution)}, 202


def serialize_attempt(attempt):
    from .job_diagnostics import build_attempt_diagnostics
    diagnostics = build_attempt_diagnostics(attempt)
    final = attempt.accounting_state in {"settled", "released"}
    cpu = 0 if attempt.accounting_state == "released" else diagnostics["cpu_core_seconds"] if final else None
    gpu = 0 if attempt.accounting_state == "released" else diagnostics["gpu_device_seconds"] if final else None
    resources = attempt.job.request.get("resources", {})
    def integer(value):
        return value if type(value) is int and 0 <= value <= 2**53 - 1 else None
    predicted = {field: integer(attempt.job.manifest.get("maximum_allocation", {}).get(field))
        for field in ("cpu_cores", "gpu_devices")}
    allocation_exceeded = any(observed is not None and predicted[field] is not None and observed > predicted[field]
        for field, observed in (("cpu_cores", diagnostics["allocated_cpus"]), ("gpu_devices", diagnostics["allocated_gpus"])))
    return {"id": str(attempt.id), "execution_request_id": str(attempt.execution_request_id),
        "state": attempt.state, "accounting_state": attempt.accounting_state,
        "native_identity": attempt.native_identity, "error_code": attempt.error_code,
        "collection_state": attempt.collection_state,
        "native_state": diagnostics["native_state"], "exit_code": diagnostics["exit_code"],
        "accounting": {"final": final, "cpu_core_seconds": cpu, "gpu_device_seconds": gpu,
            "elapsed_seconds": diagnostics["elapsed_seconds"],
            "requested": {"cpu_cores": integer(resources.get("cpus")), "gpu_devices": integer(resources.get("gpus")),
                          "time_limit_seconds": integer(resources.get("time_limit_seconds"))},
            "allocated": {"cpu_cores": diagnostics["allocated_cpus"], "gpu_devices": diagnostics["allocated_gpus"]},
            "prediction": {**predicted, "allocation_exceeded": allocation_exceeded},
            "reservation": {"cpu_core_seconds": attempt.reservation_cpu, "gpu_device_seconds": attempt.reservation_gpu},
            "reservation_difference": {"cpu_core_seconds": cpu - attempt.reservation_cpu if cpu is not None else None,
                                       "gpu_device_seconds": gpu - attempt.reservation_gpu if gpu is not None else None}}}


def serialize_job(job, *, package=False):
    from .resource_api import resource_validation
    if job.state == "archived":
        # A retained identity has no executable request. Do not reconstruct
        # resource or approval decisions from its deliberately compact metadata.
        return {"id": str(job.id), "name": job.name, "project_id": str(job.project_id),
            "project_name": job.project.name,
            "owner": {"id": str(job.project.owner_id), "username": job.project.owner.username},
            "submitted_using": {"id": str(job.credential_id), "name": job.credential.name} if job.credential_id else None,
            "state": "archived", "package_state": "archived", "archived": True, "request": job.request,
            "manifest_sha256": job.manifest_sha256, "manifest": None, "files": [],
            "execution_requests": [], "attempts": [], "approval": None,
            "resource_validation": {"valid": None, "violations": [], "effective_limits": None,
                "project_envelope_human_override_allowed": False}}
    executions = []
    approval = execution_approval(job, job.credential)
    for execution in job.execution_requests.select_related("credential").order_by("created_at"):
        decision = ApprovalDecision.objects.filter(execution_request=execution).first()
        executions.append({"id": str(execution.id), "state": execution.state, "comment": decision.comment if decision else None,
            "approval": execution.approval_requirements,
            "approval_current": execution_approval(job, execution.credential),
            "budget_reservation": execution_reservation(execution)})
    result = {"id": str(job.id), "name": job.name, "project_id": str(job.project_id),
              "project_name": job.project.name, "owner": {"id": str(job.project.owner_id), "username": job.project.owner.username},
              "submitted_using": {"id": str(job.credential_id), "name": job.credential.name} if job.credential_id else None,
              "state": job.display_state, "package_state": job.state, "request": job.request,
              "manifest_sha256": job.manifest_sha256, "execution_requests": executions,
              "attempts": [serialize_attempt(a) for a in job.attempts.select_related("connection").order_by("created_at")]}
    result["approval"] = approval
    result["resource_validation"] = resource_validation(job)
    if package or job.manifest:
        result["manifest"] = job.manifest
    if package:
        result["files"] = [{"path": entry.path, "sha256": entry.blob.sha256, "size_bytes": entry.blob.size_bytes,
            "text": read_blob(entry.blob).decode("utf-8", errors="replace") if entry.blob.size_bytes <= 1024 * 1024 else None}
            for entry in job.files.select_related("blob").order_by("path")]
    return result


def lease_work(worker, *, management_only=False, exclude_attempt_ids=(), observation_batch=1):
    if type(observation_batch) is not int or not 1 <= observation_batch <= 64:
        raise BrokerError("INVALID_REQUEST", "Observation batch must be from 1 through 64.")
    now = timezone.now()
    from compute_relay.extensions import control_names
    enabled_extensions = set(control_names()) & set(worker.supported_extensions)
    management = Q(dispatch_started=True) | Q(native_identity__isnull=False)
    submission = (Q(job__project__paused=False, connection__enabled=True, connection__archived=False)
        & (Q(job__extension_name="") | Q(job__extension_name__in=enabled_extensions)))
    with transaction.atomic():
        candidates = (Attempt.objects.select_for_update(of=("self",)).select_related("job__project", "connection")
            .filter(connection__worker=worker, next_poll_at__lte=now)
            .filter(~Q(accounting_state__in=["settled", "released"]) | Q(collection_pending=True))
            .exclude(id__in=exclude_attempt_ids)
            .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
            .filter(management if management_only else management | submission)
            .annotate(lifecycle_priority=Case(
                When(cancel_requested=True, native_identity__isnull=False, then=Value(0)),
                When(dispatch_started=True, native_identity__isnull=True, then=Value(1)),
                When(native_identity__isnull=False, then=Value(2)), default=Value(3), output_field=IntegerField()))
            # Age applies across submissions AND management. Polls become due
            # again only after their receipt; they cannot repeatedly overtake
            # an already-waiting submission. Cancellation/recovery win ties.
            .order_by("next_poll_at", "lifecycle_priority", "created_at", "id"))
        for attempt in candidates[:30]:
            entries = list(attempt.job.files.select_related("blob"))
            try:
                with transaction.atomic():
                    lease = _lease_attempt(attempt, entries, now)
            except Exception:
                _defer_lease(attempt, now)
                continue
            result = {"lease": lease}
            if observation_batch > 1 and lease["operation"] == "observe":
                used = _lease_size_bound(attempt, entries)
                observations = []
                # Do not skip over an older submission, cancellation, recovery
                # or another connection just to fill a batch.
                for extra in candidates[:observation_batch - 1]:
                    if not extra.native_identity or extra.cancel_requested or extra.connection_id != attempt.connection_id:
                        break
                    files = list(extra.job.files.select_related("blob"))
                    size = _lease_size_bound(extra, files)
                    if used + size > 8 * 1024 * 1024:
                        break
                    try:
                        with transaction.atomic():
                            additional = _lease_attempt(extra, files, now)
                    except Exception:
                        _defer_lease(extra, now)
                        continue
                    observations.append(additional)
                    used += size
                if observations:
                    result["observations"] = observations
            return result
    return {"lease": None}


def _defer_lease(attempt, now):
    # The candidate savepoint has rolled back its generation and lease. Keep
    # funds reserved, isolate its error, and let other jobs make progress.
    Attempt.objects.filter(id=attempt.id).update(error_code="LEASE_PREPARATION_FAILED",
        next_poll_at=now + timedelta(seconds=30))
    logging.getLogger(__name__).warning("Attempt lease preparation deferred")


def _lease_size_bound(attempt, entries):
    # Includes the maximum extension context plus structural JSON overhead.
    metadata = json.dumps({"manifest": attempt.job.manifest, "configuration": attempt.connection.configuration,
        "native_identity": attempt.native_identity}, ensure_ascii=True)
    context = 6 * 65536 if attempt.job.extension_name else 0
    if attempt.native_identity or attempt.dispatch_started:
        entries = []
    return len(metadata) + context + 4096 + sum((128 if item.blob.size_bytes > INLINE_BYTES
        else 4 * ((item.blob.size_bytes + 2) // 3)) + 6 * len(item.path) + 128 for item in entries)


def _lease_attempt(attempt, entries, now):
    from compute_relay.extensions import control_names
    if attempt.cancel_requested and attempt.native_identity and attempt.error_code != "CANCEL_IDENTITY_MISMATCH":
        operation = "cancel"
    elif attempt.dispatch_started and not attempt.native_identity:
        operation = "reconcile"
    else:
        operation = "observe" if attempt.native_identity else "submit"
    attempt.generation += 1
    attempt.lease_id = uuid.uuid4()
    attempt.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
    attempt.lease_deadline_at = now + timedelta(seconds=MAX_LEASE_LIFETIME_SECONDS)
    attempt.operation = operation
    if attempt.error_code == "LEASE_PREPARATION_FAILED":
        attempt.error_code = ""
    attempt.save(update_fields=["generation", "lease_id", "lease_expires_at", "lease_deadline_at", "operation", "error_code"])
    files = {item.path: {"sha256": item.blob.sha256, "size_bytes": item.blob.size_bytes}
        if item.blob.size_bytes > INLINE_BYTES else base64.b64encode(read_blob(item.blob)).decode("ascii")
        for item in entries} if operation == "submit" else {}
    lease = {"id": str(attempt.lease_id), "operation": operation, "attempt_id": str(attempt.id), "generation": attempt.generation,
        "lease_expires_at": attempt.lease_expires_at.isoformat(), "lease_deadline_at": attempt.lease_deadline_at.isoformat(),
        "connection_id": str(attempt.connection_id), "configuration": attempt.connection.configuration,
        "prepared": {"manifest": attempt.job.manifest, "manifest_sha256": attempt.job.manifest_sha256, "files": files}, "native_identity": attempt.native_identity}
    lease["log_cursors"] = {stream: {"cursor": value.get("next_cursor", 0), "file_id": value.get("file_id")}
        for stream, value in attempt.log_state.items() if stream in {"stdout", "stderr"}}
    if attempt.collection_pending and attempt.accounting_state in {"settled", "released"}:
        lease["collection_only"] = True
        lease["observation"] = attempt.observation
    if attempt.job.extension_name in control_names():
        from compute_relay.extensions import job_hook
        context = job_hook(attempt.job, "worker_lease_context", attempt)
        if context:
            if not isinstance(context, dict) or len(rfc8785.dumps(context)) > 65536:
                raise BrokerError("EXTENSION_CONTEXT_LIMIT", "Extension metadata exceeds its limit.")
            lease["extension_context"] = {attempt.job.extension_name: context}
    return lease


def checked_lease(worker, attempt_id, data, *, allow_expired=False):
    if not WorkerIdentity.objects.select_for_update().filter(id=worker.id, revoked=False).exists():
        raise BrokerError("AUTH_REQUIRED", "Worker access is revoked.", 403)
    # Project administration uses Project -> Credential. Hold the same authority
    # row before reading related state; an Attempt-only lock is insufficient on
    # the database because a concurrent pause/revocation can otherwise be missed.
    reference = Attempt.objects.filter(id=attempt_id, connection__worker=worker).values(
        "job__project_id", "grant_id", "connection_id", "execution_request__credential_id").first()
    if reference is None:
        raise BrokerError("NOT_FOUND", "Attempt not found.", 404)
    Project.objects.select_for_update().get(id=reference["job__project_id"])
    Grant.objects.select_for_update().get(id=reference["grant_id"])
    Connection.objects.select_for_update().get(id=reference["connection_id"])
    if reference["execution_request__credential_id"]:
        ProjectCredential.objects.select_for_update().get(id=reference["execution_request__credential_id"])
    attempt = Attempt.objects.select_for_update(of=("self",)).select_related("job__project", "connection", "execution_request__credential").filter(id=attempt_id, connection__worker=worker).first()
    if attempt is None:
        raise BrokerError("NOT_FOUND", "Attempt not found.", 404)
    if (str(attempt.lease_id) != data.get("lease_id") or type(data.get("generation")) is not int
            or attempt.generation != data["generation"] or not attempt.lease_expires_at
            or (not allow_expired and attempt.lease_expires_at <= timezone.now())):
        raise BrokerError("STALE_LEASE", "Lease is stale or expired.", 409)
    return attempt


def renew_lease(worker, attempt_id, data):
    """Renew only the exact current generation, within its fixed operation cap."""
    with transaction.atomic():
        attempt = checked_lease(worker, attempt_id, data, allow_expired=True)
        now = timezone.now()
        if not attempt.lease_deadline_at or attempt.lease_deadline_at <= now:
            raise BrokerError("STALE_LEASE", "The operation's maximum lease lifetime has ended.", 409)
        attempt.lease_expires_at = min(now + timedelta(seconds=LEASE_SECONDS), attempt.lease_deadline_at)
        attempt.save(update_fields=["lease_expires_at"])
        return {"renewed": True, "lease_expires_at": attempt.lease_expires_at.isoformat(),
            "lease_deadline_at": attempt.lease_deadline_at.isoformat()}


def dispatch(worker, attempt_id, data):
    with transaction.atomic():
        actor_id = Attempt.objects.filter(id=attempt_id, connection__worker=worker).values_list(
            "execution_request__credential__issued_by_id", flat=True).first()
        if actor_id:
            # NO KEY UPDATE still fences account suspension, while allowing key
            # issuance's FK checks under a project lock without a reverse wait.
            User.objects.select_for_update(no_key=True).get(id=actor_id)
        attempt = checked_lease(worker, attempt_id, data)
        if attempt.operation != "submit" or attempt.dispatch_started or attempt.state != "authorized" or attempt.accounting_state != "reserved":
            raise BrokerError("RECONCILIATION_REQUIRED", "Submission intent already exists; reconcile instead.", 409)
        credential = attempt.execution_request.credential
        if credential is None:
            raise BrokerError("HPC_IDENTITY_REQUIRED", "Execution needs a verified requesting user.", 403)
        require_credential_actor(credential)
        from .input_sources import authorize_job_inputs
        authorize_job_inputs(attempt.job, credential)
        if attempt.job.project.paused or not attempt.connection.enabled or credential.revoked or credential.expires_at <= timezone.now():
            raise BrokerError("DISPATCH_REVOKED", "Dispatch authority is no longer active.", 403)
        if "jobs:execute" not in credential.scopes or not connection_allowed(credential, attempt.connection_id):
            raise BrokerError("DISPATCH_REVOKED", "The key no longer has execution access to this cluster.", 403)
        if not attempt.job.project.connections.filter(id=attempt.connection_id).exists():
            raise BrokerError("DISPATCH_REVOKED", "The project no longer has access to this cluster.", 403)
        if data.get("manifest_sha256") != attempt.job.manifest_sha256 or data.get("connection_revision_sha256") != attempt.connection.revision_sha256:
            raise BrokerError("REVISION_CHANGED", "Package or connection binding changed.", 409)
        policy = attempt.grant.policy
        validate("project-policy.schema.json", policy)
        grant = attempt.grant
        if (grant.spent_cpu + grant.reserved_cpu > policy["allowance"]["cpu_core_seconds"]
                or grant.spent_gpu + grant.reserved_gpu > policy["allowance"]["gpu_device_seconds"]):
            raise BrokerError("BUDGET_UNAVAILABLE", "Actual usage and outstanding reservations exceed the project allowance. New dispatch is blocked.", 409)
        if datetime.fromisoformat(policy["admission_expires_at"].replace("Z", "+00:00")) <= timezone.now():
            raise BrokerError("GRANT_EXPIRED", "Dispatch cutoff has passed.", 409)
        job = attempt.job
        from compute_relay.extensions import job_hook
        job_hook(job, "validate_dispatch", job, credential, attempt=attempt)
        bound_environment(job.project, job.request)
        decision = ApprovalDecision.objects.filter(execution_request=attempt.execution_request, approved=True, consumed_at__isnull=False).first()
        if (str(attempt.connection_id) not in policy["allowed_connection_ids"]
                or job.request["environment"]["revision_sha256"] not in policy["allowed_environment_revision_sha256"]
                or (job.request["resources"].get("gpu_type") and job.request["resources"]["gpu_type"] not in policy["allowed_gpu_types"])
                or (not _within_envelope(job, policy) and not (policy["mode"] == "mixed" and decision))
                or (policy["mode"] == "approval_required" and not decision)
                or (attempt.connection.configuration.get("site_profile", {}).get("automatic_execution_allowed") is not True and not decision)
                or (credential.execution_mode == "approval_required" and not decision)):
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
        attempt.error_code = "OBSERVED_OVERAGE"


def _validate_report_fields(data):
    if (not isinstance(data, dict) or not isinstance(data.get("error_code", ""), str)
            or not isinstance(data.get("artifacts", {}), dict)
            or not isinstance(data.get("native_identity", {}), (dict, type(None)))):
        raise BrokerError("INVALID_REPORT", "Invalid worker report fields.")
    observation = data.get("observation", {})
    if not isinstance(observation, dict) or not isinstance(observation.get("accounting", {}), dict):
        raise BrokerError("INVALID_REPORT", "Observation and accounting must be objects.")
    if not isinstance(observation.get("state", ""), str):
        raise BrokerError("INVALID_REPORT", "Invalid observation state.")
    accounting = observation.get("accounting", {})
    if accounting.get("final") is True and any(type(accounting.get(key)) is not int or accounting[key] < 0
            or accounting[key] > 2**63 - 1 for key in ("cpu_core_seconds", "gpu_device_seconds")):
        raise BrokerError("INVALID_REPORT", "Final accounting requires nonnegative integer resource usage.")


def _report_replay(worker, attempt_id, data, payload_hash):
    # Serialize receipt lookup with report commits and lease replacement.
    if not WorkerIdentity.objects.select_for_update().filter(id=worker.id, revoked=False).exists():
        raise BrokerError("AUTH_REQUIRED", "Worker access is revoked.", 403)
    receipt = WorkerReport.objects.filter(lease_id=data.get("lease_id"), attempt_id=attempt_id,
        attempt__connection__worker=worker).first()
    if receipt and receipt.payload_sha256 != payload_hash:
        raise BrokerError("REPORT_CONFLICT", "Lease result was already recorded differently.", 409)
    return receipt.response if receipt else None


def report(worker, attempt_id, data):
    _validate_report_fields(data)
    extension_data = data.get("extension_data", {})
    if not isinstance(extension_data, dict) or len(rfc8785.dumps(extension_data)) > 65536:
        raise BrokerError("EXTENSION_CONTEXT_LIMIT", "Extension report metadata exceeds its limit.")
    payload_hash = digest(data)
    # Check scope before storing any result bytes. Artifact writes have separate quota transactions.
    with transaction.atomic():
        replay = _report_replay(worker, attempt_id, data, payload_hash)
        if replay is not None:
            return replay
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
            if exc.code not in {"STORAGE_QUOTA", "STORAGE_UNAVAILABLE", "OBJECT_INTEGRITY", "UPLOAD_LIMIT", "UPLOAD_BUSY"}:
                raise
            storage_failed = True
        except OSError:
            storage_failed = True
    with transaction.atomic():
        replay = _report_replay(worker, attempt_id, data, payload_hash)
        if replay is not None:
            return replay
        attempt = checked_lease(worker, attempt_id, data)
        outcome = data.get("outcome")
        try:
            validate_report_outcome(attempt.operation, outcome)
        except ValueError:
            raise BrokerError("INVALID_REPORT", "Result does not match the leased operation.", 409) from None
        if outcome == "rejected" and (attempt.native_identity or data.get("native_identity") or not attempt.dispatch_started):
            raise BrokerError("INVALID_REPORT", "A known scheduler job cannot release funds as nonaccepted.", 409)
        if data.get("native_identity"):
            if attempt.native_identity and attempt.native_identity != data["native_identity"]:
                raise BrokerError("NATIVE_IDENTITY_CHANGED", "Attempt already has a different native identity.", 409)
            attempt.native_identity = data["native_identity"]
        if attempt.operation == "cancel" and data.get("native_identity") != attempt.native_identity:
            raise BrokerError("NATIVE_IDENTITY_CHANGED", "Cancellation must name the exact leased native identity.", 409)
        previous_cancel_error = attempt.error_code
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
            if attempt.error_code in {"CANCEL_UNCONFIRMED", "CANCEL_IDENTITY_MISMATCH"}:
                attempt.error_code = ""
        elif outcome in {"cancel_unknown", "cancel_identity_mismatch"}:
            attempt.cancel_requested = True
            attempt.state = "cancel_pending"
            attempt.error_code = "CANCEL_IDENTITY_MISMATCH" if outcome == "cancel_identity_mismatch" else "CANCEL_UNCONFIRMED"
            if isinstance(data.get("observation"), dict):
                attempt.observation = data["observation"]
        else:
            observation = data.get("observation", {})
            attempt.observation = observation
            state = {"completed": "succeeded", "succeeded": "succeeded", "failed": "failed", "cancelled": "cancelled", "queued": "queued", "running": "running"}.get(observation.get("state"))
            if state:
                attempt.state = state
            accounting = observation.get("accounting", {})
            if terminal_observation(observation):
                attempt.cancel_requested = False
                if attempt.error_code in {"CANCEL_UNCONFIRMED", "CANCEL_IDENTITY_MISMATCH"}:
                    attempt.error_code = ""
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
        if "log_chunks" in data:
            from .progress import apply_logs
            apply_logs(attempt, data["log_chunks"])
        if "collection_state" in data:
            if data["collection_state"] not in {"complete", "failed"} or not terminal_observation(attempt.observation):
                raise BrokerError("INVALID_REPORT", "Collection status requires a terminal observation.", 409)
            attempt.collection_state = data["collection_state"]
            attempt.collection_pending = False
            if attempt.collection_state == "complete":
                if not storage_failed:
                    attempt.collection_failures = 0
            else:
                attempt.collection_failures += 1
                attempt.collection_pending = attempt.collection_failures < 3
                if attempt.collection_pending:
                    attempt.collection_state = "retry_pending"
            if attempt.collection_state == "complete" and attempt.error_code in {"OUTPUT_COLLECTION_FAILED", "OUTPUT_STORAGE_UNAVAILABLE", "OUTPUT_MISSING"}:
                attempt.error_code = ""
        for path, blob in artifacts.items():
            existing, created = Artifact.objects.get_or_create(attempt=attempt, path=path, defaults={"blob": blob})
            if not created and existing.blob_id != blob.id:
                raise BrokerError("ARTIFACT_CHANGED", "Final artifact bytes changed.", 409)
        if attempt.error_code != "OBSERVED_OVERAGE" and outcome not in {"cancel_unknown", "cancel_identity_mismatch"}:
            attempt.error_code = data.get("error_code", attempt.error_code)[:80]
        if storage_failed:
            if attempt.error_code != "OBSERVED_OVERAGE":
                attempt.error_code = "OUTPUT_STORAGE_UNAVAILABLE"
            if data.get("collection_state") != "failed":
                attempt.collection_failures += 1
            attempt.collection_pending = attempt.collection_failures < 3
            attempt.collection_state = "retry_pending" if attempt.collection_pending else "failed"
        attempt.lease_expires_at = None
        delay = 10 if attempt.state == "submission_uncertain" or attempt.accounting_state == "settlement_pending" else 2
        if outcome == "observed" and attempt.state in {"queued", "running"}:
            # Check short jobs promptly, then reduce per-job scheduler traffic.
            # A larger live population raises the ceiling, never the first
            # post-submit check. Queue age still prevents dispatch starvation.
            live = Attempt.objects.filter(connection__worker=worker).exclude(accounting_state__in=["settled", "released"]).count()
            ceiling = min(60, max(5, live // 2))
            if attempt.state == "running":
                ceiling = min(ceiling, 5)
            delay = min(ceiling, 2 ** min(attempt.generation, 6))
        if outcome == "cancel_unknown":
            delay = 30 if previous_cancel_error == "CANCEL_UNCONFIRMED" else 5
        elif outcome == "cancel_identity_mismatch":
            delay = 60
        if attempt.collection_pending and attempt.accounting_state in {"settled", "released"}:
            delay = 15 * max(1, attempt.collection_failures)
        if (attempt.cancel_requested and attempt.native_identity and outcome in {"submitted", "observed"}
                and attempt.error_code != "CANCEL_IDENTITY_MISMATCH"):
            delay = 0  # A cancellation arriving during observation must not inherit its backoff.
        attempt.next_poll_at = timezone.now() + timedelta(seconds=delay)
        attempt.save()
        response = {"accepted": True, "attempt": serialize_attempt(attempt)}
        WorkerReport.objects.create(lease_id=data["lease_id"], attempt=attempt, generation=attempt.generation, payload_sha256=payload_hash, response=response, extension_data=extension_data)
        # Polling an indefinitely unknown scheduler job must not grow durable
        # metadata forever. The latest receipt includes the current generation;
        # a final settled/released generation cannot receive another lease.
        # Removed generations fail checked_lease instead of executing again.
        retained = list(WorkerReport.objects.filter(attempt=attempt).order_by("-generation", "lease_id")
            .values_list("lease_id", flat=True)[:MAX_REPORT_RECEIPTS_PER_ATTEMPT])
        WorkerReport.objects.filter(attempt=attempt).exclude(lease_id__in=retained).delete()
        if attempt.job.extension_name:
            def notify_extension():
                try:
                    from compute_relay.extensions import after_report
                    after_report(attempt, extension_data)
                except Exception:
                    # The durable WorkerReport permits plugin-specific recovery.
                    # Never undo core accounting or expose exception contents.
                    logging.getLogger(__name__).warning("Extension report processing deferred")
            transaction.on_commit(notify_extension)
        return response


def cancel_attempt(attempt):
    with transaction.atomic():
        reference = Attempt.objects.values("job__project_id", "grant_id").get(id=attempt.id)
        Project.objects.select_for_update().get(id=reference["job__project_id"])
        Grant.objects.select_for_update().get(id=reference["grant_id"])
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
                if attempt.error_code in {"CANCEL_UNCONFIRMED", "CANCEL_IDENTITY_MISMATCH"}:
                    attempt.error_code = ""
            attempt.next_poll_at = timezone.now()
            attempt.save()
        return serialize_attempt(attempt), 202
