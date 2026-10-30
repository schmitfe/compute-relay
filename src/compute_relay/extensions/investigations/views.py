from django.shortcuts import get_object_or_404, render
from django.db import transaction

from compute_relay.control.auth import human_permission
from compute_relay.control.completion import wait_for_snapshot
from compute_relay.control.management import human_view
from compute_relay.control.models import Project
from compute_relay.control.views import boundary, body, idem, scope
from .models import Investigation
from . import services


@boundary("GET", "POST")
def investigations(request, project_id):
    credential, project = scope(request, "jobs:read" if request.method == "GET" else "investigations:write", project_id=project_id)
    if request.method == "POST":
        document = body(request, maximum=40000)
        return idem(request, credential, "investigation:create", document, lambda: services.create(credential, document))
    state = request.GET.get("state", "open")
    if state not in {"open", "closed", "all"}:
        raise ValueError("Invalid investigation filter")
    query = services.visible(credential)
    if state != "all":
        query = query.filter(state=state)
    cursor = int(request.GET.get("cursor", 0))
    limit = int(request.GET.get("limit", 20))
    if not 0 <= cursor <= 100000 or not 1 <= limit <= 100:
        raise ValueError("Invalid investigation pagination")
    rows = list(query.order_by("created_at", "id")[cursor:cursor + limit + 1])
    return {"investigations": [{"id": str(item.id), "name": item.name, "state": item.state,
        "created_at": item.created_at.isoformat()} for item in rows[:limit]],
        "next_cursor": cursor + limit if len(rows) > limit else None}


@boundary("GET")
def detail(request, investigation_id):
    def read():
        credential, _ = scope(request, "jobs:read")
        with transaction.atomic():
            return services.snapshot(credential, services.checked(credential, investigation_id))
    return wait_for_snapshot(read, request.GET.get("after_revision"), int(request.GET.get("wait_seconds", 0)))


@boundary("POST")
def add_jobs(request, investigation_id):
    credential, _ = scope(request, "investigations:write")
    document = body(request, maximum=8192)
    return idem(request, credential, f"investigation:{investigation_id}:add", document,
        lambda: services.add_jobs(credential, services.checked(credential, investigation_id), document))


@boundary("POST")
def finish(request, investigation_id):
    credential, _ = scope(request, "investigations:write")
    document = body(request, maximum=32768)
    return idem(request, credential, f"investigation:{investigation_id}:finish", document,
        lambda: services.finish(credential, services.checked(credential, investigation_id), document))


@human_view
def portal(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    human_permission(request.user, project, "viewer")
    return render(request, "broker_investigations/list.html", {"project": project,
        "investigations": Investigation.objects.filter(project=project).order_by("-created_at")[:100]})


@human_view
def review(request, investigation_id):
    group = get_object_or_404(Investigation.objects.select_related("project"), id=investigation_id)
    human_permission(request.user, group.project, "viewer")
    return render(request, "broker_investigations/detail.html", {"project": group.project, "investigation": group,
        "members": group.members.select_related("job").order_by("created_at", "job_id")})
