from django.shortcuts import get_object_or_404, render

from compute_broker.control.auth import human_permission, require_connection
from compute_broker.control.management import human_view
from compute_broker.control.models import Project
from compute_broker.control.views import boundary, body, idem, scope
from . import services
from .models import WorkflowRevision, WorkflowRun


def checked_run(credential, run_id):
    run = WorkflowRun.objects.select_related("revision", "credential__project").get(
        id=run_id, revision__project=credential.project)
    for node in run.revision.document["nodes"]:
        require_connection(credential, node["request"]["connection_id"])
    return run


@boundary("POST")
def revisions(request, project_id):
    credential, _ = scope(request, "workflows:write", project_id=project_id)
    document = body(request)
    return idem(request, credential, "workflow:create", document, lambda: services.create_revision(credential, document))


@boundary("POST")
def start(request, revision_id):
    credential, _ = scope(request, "workflows:write")
    document = body(request)
    if document:
        raise ValueError("Expected an empty start request")
    revision = WorkflowRevision.objects.get(id=revision_id, project=credential.project)
    return idem(request, credential, f"workflow:{revision.id}:start", document, lambda: services.start_run(credential, revision))


@boundary("GET")
def detail(request, run_id):
    credential, _ = scope(request, "jobs:read")
    run = checked_run(credential, run_id)
    # Reading status never admits new work; normal worker leases advance runs.
    services._refresh(run)
    return services.serialize_run(run)


@boundary("POST")
def advance(request, run_id):
    credential, _ = scope(request, "workflows:write")
    if body(request):
        raise ValueError("Expected an empty advance request")
    return services.advance(checked_run(credential, run_id), credential)


@boundary("POST")
def cancel(request, run_id):
    credential, _ = scope(request, "workflows:write")
    document = body(request)
    if document:
        raise ValueError("Expected an empty cancellation request")
    run = checked_run(credential, run_id)
    return idem(request, credential, f"workflow:{run.id}:cancel", document, lambda: services.cancel(run, credential))


@human_view
def portal(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    human_permission(request.user, project, "viewer")
    runs = WorkflowRun.objects.filter(revision__project=project).select_related("revision").order_by("-created_at")[:50]
    return render(request, "broker_workflows/list.html", {"project": project, "runs": runs})


@human_view
def review(request, run_id):
    run = get_object_or_404(WorkflowRun.objects.select_related("revision__project"), id=run_id)
    human_permission(request.user, run.revision.project, "viewer")
    services._refresh(run)
    return render(request, "broker_workflows/run.html", {"run": run, "project": run.revision.project, "summary": services.serialize_run(run)})
