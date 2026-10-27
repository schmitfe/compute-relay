"""Project-owned scheduled inspection and durable runtime observations."""

import json

from django.db import transaction
from django.utils import timezone

from compute_relay.control import services as core
from compute_relay.control.auth import require_connection
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Artifact, Grant, Job, Project
from compute_relay.control.storage import read_blob, store_blob

from .inventory import INSPECTION_SCRIPT, validate_runtime
from .models import EnvironmentInspection


def pending():
    return EnvironmentInspection.objects.filter(
        state__in=["draft", "prepared", "awaiting_approval", "authorized", "queued", "running", "awaiting_accounting", "awaiting_outputs"]
    ).exclude(job__state__in=["cancelled", "archived"]).exclude(job__execution_requests__state__in=["rejected", "cancelled"])


def environment_for(credential, identifier):
    environment = credential.project.environments.get(id=identifier)
    require_connection(credential, environment.connection_id)
    return environment


def serialize(inspection):
    state = inspection.state
    if inspection.job_id and not inspection.attempt_id:
        execution = inspection.job.execution_requests.order_by("-created_at", "-id").first()
        if inspection.job.state in {"cancelled", "archived"}:
            state = inspection.job.state
        elif execution:
            state = execution.state
        else:
            state = "prepared" if inspection.job.manifest else "draft"
    return {"id": str(inspection.id), "environment_id": str(inspection.environment_id), "state": state,
        "job_id": str(inspection.job_id) if inspection.job_id else None,
        "attempt_id": str(inspection.attempt_id) if inspection.attempt_id else None,
        "observation": inspection.observation, "observed_at": inspection.observed_at.isoformat() if inspection.observed_at else None}


def create(credential, environment):
    from compute_relay.control.metadata import reserve_metadata
    core.bound_environment(credential.project, {"connection_id": str(environment.connection_id),
        "environment": {"id": str(environment.id), "revision_sha256": environment.revision_sha256}})
    with transaction.atomic():
        Project.objects.select_for_update().get(id=credential.project_id)
        existing = pending().filter(credential=credential, environment=environment).order_by("-created_at").first()
        if existing:
            return serialize(existing), 200
        inspection = EnvironmentInspection.objects.create(project=credential.project, credential=credential, environment=environment)
        reserve_metadata(credential.project, "environment_inspection", inspection.id, 40000)
    return serialize(inspection), 201


def prepare(inspection, resources=None, slurm=None):
    from .requests import defaults, require_scheduler_options
    environment = inspection.environment
    reference = {"id": str(environment.id), "revision_sha256": environment.revision_sha256}
    files = {"_environment/runtime_inventory.py": INSPECTION_SCRIPT.encode(),
        "_environment/inspection.json": json.dumps({"inspection_id": str(inspection.id), "environment": reference}).encode()}
    sources = {path: store_blob(inspection.project, content) for path, content in files.items()}
    request = {"schema_version": "1.0", "project_id": str(inspection.project_id), "connection_id": str(environment.connection_id),
        "name": "inspect-" + str(inspection.id)[:12], "environment": reference,
        "command": {"interpreter": "python3", "script": "_environment/runtime_inventory.py", "arguments": []},
        "outputs": ["_environment/inventory.json"],
        "files": [{"path": path, "sha256": blob.sha256, "size_bytes": blob.size_bytes, "executable": False} for path, blob in sources.items()]}
    with transaction.atomic():
        # Revisions and admissions share the project lock. A concurrent retry
        # cannot replace a probe once another caller has requested execution.
        Project.objects.select_for_update().get(id=inspection.project_id)
        current = EnvironmentInspection.objects.select_for_update().get(id=inspection.id)
        previous = current.job if current.job_id else None
        if previous and previous.state in {"cancelled", "archived"}:
            raise BrokerError("INSPECTION_CLOSED", "Request a fresh probe with a new idempotency key.", 409)
        saved = previous.request if previous else {}
        options = saved.get("slurm", {}) if slurm is None else {**saved.get("slurm", {}), **slurm}
        selected = saved.get("resources") if resources is None else resources
        requested = bool(previous and previous.execution_requests.exists())
        if requested:
            if selected != saved["resources"] or options != saved["slurm"]:
                raise BrokerError("INSPECTION_ALREADY_REQUESTED", "Cancel the existing probe before requesting different settings.", 409)
        else:
            selected, options = defaults(environment.connection, selected, options,
                project_policy=Grant.objects.get(project_id=inspection.project_id).policy)
            require_scheduler_options(environment.connection, options)
        request.update(resources=selected, slurm=options)
        if not previous or selected != saved["resources"] or options != saved["slurm"]:
            result, _ = core.create_job(inspection.credential, request, previous=previous, trusted_files=sources)
            job = Job.objects.get(id=result["id"])
            job.extension_name = "environments"
            job.save(update_fields=["extension_name"])
            current.job = job
            current.state = "draft"
            current.save(update_fields=["job", "state"])
    inspection.refresh_from_db()
    job = inspection.job
    core.prepare_job(job)
    EnvironmentInspection.objects.filter(id=inspection.id, job=job, state__in=["draft", "prepared"]).update(state="prepared")
    inspection.refresh_from_db()
    if inspection.job_id != job.id:
        raise BrokerError("INSPECTION_CHANGED", "The probe was revised concurrently; read its inventory and retry.", 409)


@transaction.atomic
def request_execution(inspection, credential):
    Project.objects.select_for_update().get(id=inspection.project_id)
    current = EnvironmentInspection.objects.select_for_update().get(id=inspection.id)
    if current.credential_id != credential.id:
        raise BrokerError("NOT_FOUND", "Inspection not found.", 404)
    if current.job_id != inspection.job_id:
        raise BrokerError("INSPECTION_CHANGED", "The probe was revised concurrently; read its inventory and retry.", 409)
    existing = current.job.execution_requests.order_by("-created_at", "-id").first()
    if existing:
        attempt = core.Attempt.objects.filter(execution_request=existing).first()
        return {"id": str(existing.id), "state": existing.state, "attempt_id": str(attempt.id) if attempt else None}
    # Fence execution by inspection identity, including resumptions that use a
    # different client key. The current immutable job is pinned by this lock.
    execution, _ = core.idempotent(current.project, "inspection:execute:" + str(current.id), "execution", {},
        lambda: core.request_execution(current.job, credential))
    if execution["state"] == "awaiting_approval":
        EnvironmentInspection.objects.filter(id=current.id).update(state="awaiting_approval")
    return execution


def sync(attempt):
    inspection = EnvironmentInspection.objects.filter(job=attempt.job).select_related("environment").first()
    if inspection is None:
        return False
    inspection.attempt = attempt
    inspection.state = attempt.state
    if attempt.state == "succeeded" and attempt.accounting_state != "settled":
        inspection.state = "awaiting_accounting"
    if attempt.state == "succeeded" and attempt.accounting_state == "settled":
        artifact = Artifact.objects.filter(attempt=attempt, path="_environment/inventory.json").select_related("blob").first()
        if artifact and artifact.blob.size_bytes <= 32768:
            try:
                observation = json.loads(read_blob(artifact.blob))
                if (observation["inspection_id"] != str(inspection.id) or observation["attempt_id"] != str(attempt.id)
                        or observation["environment"] != {"id": str(inspection.environment_id), "revision_sha256": inspection.environment.revision_sha256}
                        or observation["scope"] != "compute_node"):
                    raise ValueError("Inspection identity mismatch")
                validate_runtime(observation["runtime"])
                inspection.observation = observation
                inspection.observed_at = timezone.now()
                inspection.state = "ready"
            except (ValueError, KeyError, TypeError):
                inspection.state = "invalid_inventory"
        else:
            inspection.state = "awaiting_outputs" if attempt.collection_pending else "inventory_missing"
    inspection.save()
    return True


def latest(environment, project):
    return EnvironmentInspection.objects.filter(environment=environment, project=project, state="ready").order_by("-observed_at", "-id").first()
