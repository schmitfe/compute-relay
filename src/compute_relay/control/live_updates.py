"""Read-only human portal snapshots; polling never contacts a worker or scheduler."""

from uuid import uuid4

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect

from . import services
from .auth import human_permission
from .errors import BrokerError
from .execution_policy import approval_requirements
from .local_auth import current_user_active
from .job_diagnostics import build_attempt_diagnostics
from .models import Connection, ExecutionRequest, Grant, Job, Project
from .resource_api import job_approval_requirements, resource_validation


def _projects(user, role):
    return Project.objects.filter(owner=user)


def _render(request, name, context):
    return render_to_string("broker/" + name + ".html", context, request=request)


def _pending(request, projects):
    pending = ExecutionRequest.objects.filter(job__project__in=projects,
        state="awaiting_approval").select_related("job__project").order_by("created_at", "id")
    return _render(request, "live_pending_reviews", {"pending_count": pending.count(), "pending": pending[:20]})


def _jobs(request, projects):
    jobs = Job.objects.with_summary().filter(project__in=projects).order_by("-created_at", "-id")[:100]
    return _render(request, "job_list", {"jobs": jobs})


def _dashboard(request):
    return {"fragments": {
        "live-job-list": _jobs(request, _projects(request.user, "viewer")),
        "live-review-notifications": _pending(request, _projects(request.user, "approver")),
    }}


def _project(request, object_id):
    project = Project.objects.get(id=object_id)
    human_permission(request.user, project, "project_admin")
    return {"fragments": {
        "live-job-list": _jobs(request, [project]),
        "live-review-notifications": _pending(request, [project]),
        "live-project-allowance": _render(request, "live_project_allowance", {"allowance": services.allowance(project)}),
    }}


def _review(request, object_id):
    job = Job.objects.with_summary().get(id=object_id)
    human_permission(request.user, job.project, "approver")
    # Do not retrieve package files, source scripts or object-store content just
    # to update status. History queries are bounded independently of its size.
    policy = Grant.objects.get(project_id=job.project_id).policy
    connection = Connection.objects.filter(id=job.request.get("connection_id")).first()
    detail = {"manifest_sha256": job.manifest_sha256,
        "resource_validation": resource_validation(job, policy=policy, connection=connection),
        "approval": job_approval_requirements(job, policy=policy, connection=connection)}
    within = None if not (job.manifest or {}).get("maximum_allocation") else not any(
        row["source"] == "project" for row in detail["resource_validation"]["violations"])
    executions = job.execution_requests.annotate(
        _live_updated_at=Coalesce("approvaldecision__created_at", "created_at"),
        _decision_comment=F("approvaldecision__comment"),
        _credential_execution_mode=F("credential__execution_mode"),
    ).values("id", "state", "created_at", "_live_updated_at", "_decision_comment", "_credential_execution_mode",
        "approval_requirements", "reservation_state", "reservation_cpu", "reservation_gpu")
    recent_requests = list(executions.order_by("-_live_updated_at", "-id")[:100])
    awaiting_requests = list(executions.filter(state="awaiting_approval").order_by("created_at", "id")[:100])
    # A recent decision on an old request must update its panel too. Keep older
    # pending requests without scanning every historical request/decision.
    requests_by_id = {execution["id"]: execution for execution in recent_requests + awaiting_requests}
    visible_requests = sorted(requests_by_id.values(), key=lambda execution: (execution["created_at"], str(execution["id"])))
    attempts = job.attempts.select_related("connection").order_by("-created_at", "-id")
    unresolved = ~Q(state__in=services.TERMINAL) | ~Q(accounting_state__in=["settled", "released"])
    # An older running attempt must keep moving even if this job already has
    # many finished attempts. Retain existing older history in the browser.
    changing = list(attempts.filter(unresolved)[:100])
    recent = list(attempts.exclude(unresolved)[:10])
    visible_attempts = sorted(changing + recent, key=lambda attempt: (attempt.created_at, str(attempt.id)), reverse=True)

    def execution_context(execution):
        current = approval_requirements(policy, execution["_credential_execution_mode"] or "inherit",
            connection.configuration if connection else {}, within_envelope=within)
        return {"id": str(execution["id"]), "state": execution["state"], "comment": execution["_decision_comment"],
            "approval": execution["approval_requirements"], "approval_current": current,
            "budget_reservation": {"state": execution["reservation_state"],
                "cpu_core_seconds": execution["reservation_cpu"], "gpu_device_seconds": execution["reservation_gpu"]}}

    return {"fragments": {
        "live-job-summary": _render(request, "live_job_summary", {"job": job}),
        "live-review-allowance": _render(request, "live_review_allowance", {"allowance": services.allowance(job.project)}),
        "live-resource-validation": _render(request, "resource_validation", {"validation": detail["resource_validation"]}),
        "live-approval-requirements": _render(request, "approval_requirements", {"requirements": detail["approval"]}),
    }, "execution_requests": [{"id": str(execution["id"]), "state": execution["state"],
        "html": _render(request, "live_execution_request", {"job": job, "detail": detail,
            "execution": execution_context(execution),
            "idempotency_key": str(uuid4())})}
        for execution in visible_requests],
        "attempts": [{"id": str(attempt.id), "html": _render(request, "live_attempt", {
            "diagnostics": build_attempt_diagnostics(attempt)})}
            for attempt in visible_attempts]}


def _cluster(request, object_id):
    clusters = Connection.objects.filter(archived=False, owner=request.user).select_related("worker")
    cluster = clusters.get(id=object_id)
    return {"fragments": {"live-cluster-status": _render(request, "live_cluster_status", {"cluster": cluster})}}


@csrf_protect
def snapshot(request, kind, object_id=None):
    """Return only fixed fragments after current session/role authorization."""
    try:
        if request.method != "GET":
            raise BrokerError("METHOD_NOT_ALLOWED", "Live updates are read-only.", 405)
        if not request.user.is_authenticated:
            raise BrokerError("AUTH_REQUIRED", "Sign in to receive live updates.", 401)
        if not request.user.is_active or not current_user_active(request.user):
            raise BrokerError("AUTH_REQUIRED", "Account verification is required.", 403)
        if kind == "dashboard" and object_id is None:
            payload = _dashboard(request)
        elif kind in {"project", "review", "cluster"} and object_id is not None:
            payload = {"project": _project, "review": _review, "cluster": _cluster}[kind](request, object_id)
        else:
            raise BrokerError("NOT_FOUND", "This live view is unavailable.", 404)
        response = JsonResponse(payload)
    except BrokerError as exc:
        response = JsonResponse({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)
    except (ObjectDoesNotExist, ValueError):
        response = JsonResponse({"error": {"code": "NOT_FOUND", "message": "This live view is unavailable."}}, status=404)
    response["Cache-Control"] = "no-store, private"
    response["Pragma"] = "no-cache"
    response["Vary"] = "Cookie"
    response["X-Content-Type-Options"] = "nosniff"
    if response.status_code == 405:
        response["Allow"] = "GET"
    return response
