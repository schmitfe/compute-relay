"""Finite plans, metadata grouping and reconciliation through core records."""

from django.db import transaction
from django.utils import timezone

from compute_relay.control.auth import allowed_connections, require_connection
from compute_relay.control.completion import job_snapshot
from compute_relay.control.errors import BrokerError
from compute_relay.control.metadata import json_size, reserve_metadata
from compute_relay.control.models import Connection, Job, Project
from .models import Investigation, InvestigationConnection, InvestigationJob

ROLES = {"core", "validation", "exploration"}


def validate_plan(plan):
    required = {"question", "deliverable", "core_experiments", "required_validation", "stopping_rule", "budget", "diagnostic_limit"}
    if not isinstance(plan, dict) or set(plan) != required or json_size(plan) > 32768:
        raise BrokerError("INVALID_PLAN", "Supply the question, deliverable, core work, validation, stopping rule and finite budget.")
    for key in ("question", "deliverable", "stopping_rule"):
        if not isinstance(plan[key], str) or not 1 <= len(plan[key].strip()) <= 4000:
            raise BrokerError("INVALID_PLAN", "Plan text must contain 1 through 4000 characters.")
    for key in ("core_experiments", "required_validation"):
        if (not isinstance(plan[key], list) or not (1 if key == "core_experiments" else 0) <= len(plan[key]) <= 32
                or any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 512 for item in plan[key])):
            raise BrokerError("INVALID_PLAN", "List bounded experiment and validation criteria.")
    budget = plan["budget"]
    if (not isinstance(budget, dict) or set(budget) != {"cpu_core_seconds", "gpu_device_seconds", "max_executions"}
            or any(type(budget[key]) is not int or not 0 <= budget[key] <= 2**53 - 1 for key in budget)
            or not 1 <= budget["max_executions"] <= 10000
            or type(plan["diagnostic_limit"]) is not int or not 0 <= plan["diagnostic_limit"] <= 32):
        raise BrokerError("INVALID_PLAN", "Use finite resource-seconds, a positive execution cap and 0 through 32 diagnostics.")


def require_write(credential):
    from compute_relay.control.auth import require_credential_actor
    Project.objects.select_for_update().get(id=credential.project_id)
    credential.refresh_from_db()
    require_credential_actor(credential)
    if credential.revoked or credential.expires_at <= timezone.now() or "investigations:write" not in credential.scopes:
        raise BrokerError("SCOPE_DENIED", "Investigation changes require investigations:write.", 403)


@transaction.atomic
def create(credential, document):
    require_write(credential)
    if set(document) != {"name", "plan"} or not isinstance(document["name"], str) or not 1 <= len(document["name"].strip()) <= 80:
        raise BrokerError("INVALID_PLAN", "Use a name of 1 through 80 characters and a plan.")
    validate_plan(document["plan"])
    connections = list(allowed_connections(credential).values_list("id", flat=True))
    if not connections:
        raise BrokerError("CONNECTION_DENIED", "An investigation requires access to at least one cluster.", 403)
    group = Investigation.objects.create(project=credential.project, name=document["name"], plan=document["plan"])
    InvestigationConnection.objects.bulk_create([
        InvestigationConnection(investigation=group, connection_id=connection_id) for connection_id in connections])
    _resize(group)
    return {"id": str(group.id), "name": group.name, "state": group.state,
        "plan_limits": "agent_guidance; project_grant_enforces_compute_admission"}, 201


def visible(credential):
    inaccessible = Connection.objects.exclude(id__in=allowed_connections(credential))
    return Investigation.objects.filter(project=credential.project).exclude(
        connection_scope__connection__in=inaccessible)


def require_access(credential, group):
    if not visible(credential).filter(id=group.id).exists():
        raise BrokerError("CONNECTION_DENIED", "This key cannot access every cluster in the investigation's scope.", 403)


def checked(credential, identifier):
    group = Investigation.objects.get(id=identifier, project=credential.project)
    require_access(credential, group)
    return group


def filter_jobs(query, credential, identifier):
    group = checked(credential, identifier)
    return query.filter(id__in=group.members.values("job_id"))


def validate_related_execution(job, credential):
    member = InvestigationJob.objects.select_related("investigation").filter(job=job).first()
    if member:
        require_access(credential, member.investigation)
    if member and member.investigation.state == "closed":
        raise BrokerError("INVESTIGATION_CLOSED", "This investigation's scope is closed; no new execution request is permitted.", 409)


def _resize(group):
    reserve_metadata(group.project, "investigation", group.id,
        json_size({"name": group.name, "plan": group.plan}) + len(group.report.encode())
        + 2048 + 512 * group.members.count() + 128 * group.connections.count())


@transaction.atomic
def bind_job(job, credential, context):
    require_write(credential)
    if not isinstance(context, dict) or set(context) - {"id", "role"} or "id" not in context or context.get("role", "core") not in ROLES:
        raise BrokerError("INVALID_PLAN", "Use an investigation ID and core, validation or exploration role.")
    group = Investigation.objects.select_for_update().get(id=context["id"], project=credential.project)
    require_access(credential, group)
    require_connection(credential, job.request["connection_id"])
    if not group.connections.filter(id=job.request["connection_id"]).exists():
        raise BrokerError("CONNECTION_DENIED", "The job's cluster is outside this investigation's fixed scope.", 403)
    prior = InvestigationJob.objects.filter(job=job).first()
    if prior:
        if prior.investigation_id != group.id or prior.role != context.get("role", "core"):
            raise BrokerError("INVESTIGATION_CHANGED", "This job is already assigned to a different investigation or role.", 409)
        return
    if group.state != "open" or group.members.count() >= 128:
        raise BrokerError("INVESTIGATION_CLOSED", "This investigation is closed or has reached its 128-job limit.", 409)
    InvestigationJob.objects.create(investigation=group, job=job, role=context.get("role", "core"))
    _resize(group)


def after_revision(job, previous, credential):
    member = InvestigationJob.objects.filter(job=previous).first()
    if member:
        bind_job(job, credential, {"id": str(member.investigation_id), "role": member.role})


@transaction.atomic
def add_jobs(credential, group, document):
    require_write(credential)
    if set(document) != {"jobs"} or not isinstance(document["jobs"], list) or not 1 <= len(document["jobs"]) <= 32:
        raise BrokerError("INVALID_REQUEST", "Attach 1 through 32 explicit jobs.")
    for item in document["jobs"]:
        if not isinstance(item, dict) or set(item) != {"job_id", "role"}:
            raise BrokerError("INVALID_REQUEST", "Each member needs job_id and role.")
        job = Job.objects.get(id=item["job_id"], project=credential.project)
        bind_job(job, credential, {"id": str(group.id), "role": item["role"]})
    return {"accepted": True, "id": str(group.id)}, 200


def snapshot(credential, group):
    require_access(credential, group)
    summaries, jobs = [], []
    for member in group.members.select_related("job").order_by("created_at", "job_id"):
        require_connection(credential, member.job.request["connection_id"])
        item = job_snapshot(credential, member.job, limit=1)
        summaries.append(item["summary"])
        jobs.append({**item["job"], "role": member.role, "snapshot_revision": item["snapshot_revision"],
            "summary": item["summary"]})
    usage = {field: sum(item["spent"][field] for item in summaries) for field in ("cpu_core_seconds", "gpu_device_seconds")}
    reserved = {field: sum(item["reserved"][field] for item in summaries) for field in usage}
    attempts = sum(item["attempts"] for item in summaries)
    pending = sum(item["pending_approvals"] for item in summaries)
    diagnostics = sum(item["attempts"] + item["pending_approvals"]
        for item, row in zip(summaries, jobs) if row["role"] == "exploration")
    budget = group.plan["budget"]
    plan_exhausted = attempts + pending >= budget["max_executions"] or any(
        usage[field] + reserved[field] >= budget[field] if budget[field] > 0
        else usage[field] + reserved[field] > 0 for field in usage)
    return {"schema_version": "1.0", "id": str(group.id), "project_id": str(group.project_id), "name": group.name,
        "connection_ids": sorted(str(identifier) for identifier in group.connections.values_list("id", flat=True)),
        "state": group.state, "plan": group.plan, "conclusion": group.conclusion, "report": group.report,
        "plan_limits": "agent_guidance; project_grant_enforces_compute_admission",
        "summary": {"job_count": len(jobs), "attempts": attempts, "pending_approvals": pending,
            "diagnostic_executions": diagnostics, "diagnostic_limit_reached": diagnostics >= group.plan["diagnostic_limit"],
            "spent": usage, "reserved": reserved,
            "plan_limit_reached": plan_exhausted, "operationally_reconciled": all(item["operationally_reconciled"] for item in summaries),
            "unexecuted_packages": sum(not item["execution_requests"] and row["package_state"] not in {"cancelled", "archived"}
                for item, row in zip(summaries, jobs)),
            "unacknowledged_attempts": sum(item["unacknowledged_attempts"] for item in summaries),
            "local_hash_verification": "not_observed_by_broker", "scientific_validation": "agent_report_only",
            "monetary_cost": "not_configured"}, "jobs": jobs}


@transaction.atomic
def finish(credential, group, document):
    require_write(credential)
    if (set(document) != {"conclusion", "report", "cancel_unstarted"}
            or document["conclusion"] not in {"sufficient", "inconclusive", "user_stopped"}
            or not isinstance(document["report"], str) or not 1 <= len(document["report"].strip()) <= 16000
            or type(document["cancel_unstarted"]) is not bool):
        raise BrokerError("INVALID_REQUEST", "Record sufficient, inconclusive or user_stopped, a report and cancel_unstarted.")
    group = Investigation.objects.select_for_update().get(id=group.id, project=credential.project)
    require_access(credential, group)
    if group.state == "closed":
        if (group.conclusion, group.report) != (document["conclusion"], document["report"]):
            raise BrokerError("INVESTIGATION_CLOSED", "This investigation already has a different closing report.", 409)
        return {"id": str(group.id), "state": group.state}, 200
    if document["cancel_unstarted"]:
        if "jobs:cancel" not in credential.scopes:
            raise BrokerError("SCOPE_DENIED", "Cancelling unstarted work requires jobs:cancel.", 403)
        from compute_relay.control import services as core
        for member in group.members.select_related("job"):
            job = member.job
            require_connection(credential, job.request["connection_id"])
            for execution in job.execution_requests.filter(state="awaiting_approval"):
                core.cancel_execution_request(execution)
            # An authorized attempt is already admitted work. Only the explicit
            # attempt-cancellation API may cancel it, including uncertain dispatch.
            if not job.execution_requests.exists() and job.state in {"draft", "prepared"}:
                job.state = "cancelled"
                job.save(update_fields=["state"])
    group.state, group.conclusion, group.report, group.closed_at = "closed", document["conclusion"], document["report"], timezone.now()
    group.save(update_fields=["state", "conclusion", "report", "closed_at"])
    _resize(group)
    return {"id": str(group.id), "state": group.state, "execution_cancelled": False,
        "reconcile_tool": "get_investigation", "scientific_validation": "agent_report_only"}, 200
