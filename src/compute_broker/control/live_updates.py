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
from .identity.authority import cached_user_active
from .job_diagnostics import build_attempt_diagnostics
from .models import Connection, ExecutionRequest, Job, Membership, Project
from .resource_api import resource_validation


def _projects(user, role):
    # Match the existing dashboard's owner/member visibility, while rechecking
    # the role on each request. JSON membership roles work on SQLite and PG.
    accepted = {role, "project_admin"}
    if role == "viewer":
        accepted.update({"submitter", "approver"})
    member_ids = [membership.project_id for membership in Membership.objects.filter(user=user)
                  if set(membership.roles) & accepted]
    return Project.objects.filter(Q(owner=user) | Q(id__in=member_ids)).distinct()


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
    detail = {"manifest_sha256": job.manifest_sha256, "resource_validation": resource_validation(job)}
    executions = job.execution_requests.annotate(
        _live_updated_at=Coalesce("approvaldecision__created_at", "created_at"),
        _decision_comment=F("approvaldecision__comment"),
    ).values("id", "state", "created_at", "_live_updated_at", "_decision_comment")
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
    return {"fragments": {
        "live-job-summary": _render(request, "live_job_summary", {"job": job}),
        "live-review-allowance": _render(request, "live_review_allowance", {"allowance": services.allowance(job.project)}),
        "live-resource-validation": _render(request, "resource_validation", {"validation": detail["resource_validation"]}),
    }, "execution_requests": [{"id": str(execution["id"]), "state": execution["state"],
        "html": _render(request, "live_execution_request", {"job": job, "detail": detail,
            "execution": {"id": str(execution["id"]), "state": execution["state"], "comment": execution["_decision_comment"]},
            "idempotency_key": str(uuid4())})}
        for execution in visible_requests],
        "attempts": [{"id": str(attempt.id), "html": _render(request, "live_attempt", {
            "diagnostics": build_attempt_diagnostics(attempt)})}
            for attempt in visible_attempts]}


def _cluster(request, object_id):
    clusters = Connection.objects.filter(archived=False).select_related("worker")
    if not request.user.is_superuser:
        clusters = clusters.filter(owner=request.user)
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
        if not request.user.is_active or not cached_user_active(request.user):
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
