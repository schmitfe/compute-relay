"""Project-owned scheduled inspection and durable runtime observations."""

import json

from django.db import transaction
from django.utils import timezone

from compute_relay.control import services as core
from compute_relay.control.auth import require_connection
from compute_relay.control.models import Artifact, Job
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
        elif execution and execution.state in {"rejected", "cancelled"}:
            state = execution.state
    return {"id": str(inspection.id), "environment_id": str(inspection.environment_id), "state": state,
        "job_id": str(inspection.job_id) if inspection.job_id else None,
        "attempt_id": str(inspection.attempt_id) if inspection.attempt_id else None,
        "observation": inspection.observation, "observed_at": inspection.observed_at.isoformat() if inspection.observed_at else None}


def create(credential, environment, resources, slurm):
    from compute_relay.control.metadata import reserve_metadata
    core.bound_environment(credential.project, {"connection_id": str(environment.connection_id),
        "environment": {"id": str(environment.id), "revision_sha256": environment.revision_sha256}})
    with transaction.atomic():
        from compute_relay.control.models import Project
        Project.objects.select_for_update().get(id=credential.project_id)
        existing = pending().filter(credential=credential, environment=environment).order_by("-created_at").first()
        if existing:
            return serialize(existing), 200
        inspection = EnvironmentInspection.objects.create(project=credential.project, credential=credential, environment=environment)
        reserve_metadata(credential.project, "environment_inspection", inspection.id, 40000)
    return serialize(inspection), 201


def prepare(inspection, resources, slurm):
    if inspection.job_id:
        core.prepare_job(inspection.job)
        return
    environment = inspection.environment
    reference = {"id": str(environment.id), "revision_sha256": environment.revision_sha256}
    files = {"_environment/runtime_inventory.py": INSPECTION_SCRIPT.encode(),
        "_environment/inspection.json": json.dumps({"inspection_id": str(inspection.id), "environment": reference}).encode()}
    sources = {path: store_blob(inspection.project, content) for path, content in files.items()}
    request = {"schema_version": "1.0", "project_id": str(inspection.project_id), "connection_id": str(environment.connection_id),
        "name": "inspect-" + str(inspection.id)[:12], "environment": reference,
        "command": {"interpreter": "python3", "script": "_environment/runtime_inventory.py", "arguments": []},
        "resources": resources, "slurm": slurm, "outputs": ["_environment/inventory.json"],
        "files": [{"path": path, "sha256": blob.sha256, "size_bytes": blob.size_bytes, "executable": False} for path, blob in sources.items()]}
    with transaction.atomic():
        current = EnvironmentInspection.objects.select_for_update().get(id=inspection.id)
        if not current.job_id:
            result, _ = core.create_job(inspection.credential, request, trusted_files=sources)
            job = Job.objects.get(id=result["id"])
            job.extension_name = "environments"
            job.save(update_fields=["extension_name"])
            current.job = job
            current.state = "prepared"
            current.save(update_fields=["job", "state"])
    inspection.refresh_from_db()
    core.prepare_job(inspection.job)


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
