"""Human configuration and project bearer dry-runs; no data-plane operations."""

import json
from decimal import Decimal
from uuid import UUID
from urllib.parse import urlencode

from django.db.models import Count
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from compute_relay.control.auth import worker_identity
from compute_relay.control.errors import BrokerError
from compute_relay.control.local_auth import current_user_active
from compute_relay.control.management import human_view, managed_project
from compute_relay.control.models import Project
from compute_relay.control.views import body, boundary, scope
from . import services
from .forms import GIB, ProjectStorageForm, StorageTargetForm
from .models import (ProjectStorageBinding, ProjectStorageBudget, StorageCapacityPool,
                     StorageTarget, StorageTargetRegistration)
from .planning import ROLES, StoragePlanningError


@human_view
def targets(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    if not current_user_active(request.user):
        raise BrokerError("AUTH_REQUIRED", "Account verification is required.", 403)
    projects = Project.objects.filter(owner=request.user)
    projects = list(projects.order_by("name", "id"))
    counts = {row["project_id"]: row["total"] for row in StorageTarget.objects.filter(project__in=projects)
              .values("project_id").annotate(total=Count("id"))}
    return render(request, "broker_storage/targets.html", {"projects": [
        {"project": project, "target_count": counts.get(project.id, 0)} for project in projects]})


def _project_context(request, project_id):
    document = request.POST if request.method == "POST" else request.GET
    explicit_ids = [value for field in ("project", "project_id") for value in document.getlist(field) if value]
    if any(str(value) != str(project_id) for value in explicit_ids):
        raise BrokerError("STORAGE_PROJECT_DENIED", "This storage form belongs to another project.", 403)
    return managed_project(request, project_id)


def _storage_path(project, suffix=""):
    return f"/projects/{project.id}/storage/" + suffix


def _setup_connections(user, project):
    return project.connections.filter(owner=user, enabled=True, archived=False).order_by("name", "id")


@human_view
def target_new(request, project_id):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    project = _project_context(request, project_id)
    initial = {"connection": request.GET.get("connection"), "root_path": request.GET.get("root_path", ""),
        "name": request.GET.get("name", ""), "pool": request.GET.get("pool") or None,
        "enable_probes": request.GET.get("enable_probes") == "true"}
    if request.GET.getlist("roles"):
        initial["roles"] = request.GET.getlist("roles")
    if request.GET.get("submission_id"):
        initial["submission_id"] = request.GET["submission_id"]
    form = StorageTargetForm(request.POST if request.method == "POST" else None, user=request.user,
                             project=project, initial=initial)
    if request.method == "POST" and request.POST.get("action") == "browse_directory" and form.personal:
        return redirect(_storage_path(project, f"targets/directories/{form.selected_connection.id}/?") +
                        urlencode(_retained_selection(request.POST), doseq=True))
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if form.personal:
            registration = services.queue_personal_target(request.user, data["connection"], data["root_path"],
                data["name"], data["roles"], data["connection_revision"], data["submission_id"], project=project)
            return redirect(_storage_path(project, f"targets/registrations/{registration.id}/"))
        services.register_target(request.user, data["connection"], data["root_alias"], data["name"],
                                 project=project, pool=data.get("pool"), enable_probes=data["enable_probes"])
        return redirect(_storage_path(project))
    return render(request, "broker_storage/target_form.html", {"project": project, "form": form, "cluster": form.selected_connection,
        "clusters": _setup_connections(request.user, project)})


def _retained_selection(document):
    """Retain human form choices, never credentials or arbitrary return URLs."""
    return {"name": document.get("name", "")[:100], "pool": document.get("pool", "")[:40],
        "root_path": document.get("root_path", "")[:240],
        "enable_probes": "true" if document.get("enable_probes") in {"true", "on"} else "false",
        "roles": [role for role in document.getlist("roles") if role in ROLES],
        "submission_id": document.get("submission_id", "")[:40]}


@human_view
def target_directories(request, connection_id, project_id):
    from compute_relay.control.connector_setup import enqueue, own_connection
    from compute_relay.control.cluster_setup import canonical_root
    from compute_relay.control.errors import BrokerError
    from compute_relay.control.models import ConnectorTask
    project = _project_context(request, project_id)
    connection = get_object_or_404(_setup_connections(request.user, project), id=connection_id)
    connection = own_connection(request.user, connection.id)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    selected = _retained_selection(request.POST if request.method == "POST" else request.GET)
    if request.method == "POST":
        path = request.POST.get("path", "")
        if path and not canonical_root(path) and path != "/":
            raise BrokerError("INVALID_DIRECTORY", "Choose a canonical absolute directory.")
        task = enqueue(request.user, connection.id, "browse", {"path": path, "storage_project_id": str(project.id)},
                       request.POST.get("connection_revision"))
        return redirect(_storage_path(project, f"targets/directories/{connection.id}/?") +
                        urlencode({**selected, "task": str(task.id)}, doseq=True))
    task = None
    if request.GET.get("task"):
        try:
            identifier = UUID(request.GET["task"])
        except ValueError:
            raise BrokerError("INVALID_TASK", "Choose a valid directory request.") from None
        task = get_object_or_404(ConnectorTask, id=identifier, owner=request.user, connection=connection, kind="browse")
        if task.request.get("storage_project_id") != str(project.id):
            from django.http import Http404
            raise Http404("Directory request not found.")
        from django.utils import timezone
        if task.expires_at <= timezone.now() and task.state in {"queued", "running"}:
            task.state, task.error_code = "failed", "CONNECTOR_TASK_EXPIRED"
    result = task.result if task and task.state == "succeeded" else {}
    select_url = _storage_path(project, "targets/new/?") + urlencode({**selected, "connection": str(connection.id),
        "root_path": result.get("path", "")}, doseq=True)
    return_url = _storage_path(project, "targets/new/?") + urlencode({**selected, "connection": str(connection.id)}, doseq=True)
    return render(request, "broker_storage/target_directories.html", {"project": project, "cluster": connection, "task": task,
        "result": result, "selection": selected, "select_url": select_url, "return_url": return_url,
        "connector_ready": "personal_setup_v1" in connection.worker.connector_features,
        "waiting": bool(task and task.state in {"queued", "running"})})


@human_view
def target_registration(request, registration_id, project_id):
    from compute_relay.control.connector_setup import own_connection
    from django.utils import timezone
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    registration = get_object_or_404(StorageTargetRegistration.objects.select_related("task", "connection"),
        id=registration_id, owner=request.user)
    project = _project_context(request, project_id)
    if registration.project_id != project.id:
        from django.http import Http404
        raise Http404("Storage directory request not found.")
    get_object_or_404(_setup_connections(request.user, project), id=registration.connection_id)
    own_connection(request.user, registration.connection_id)
    if request.method == "POST":
        services.finish_personal_target(request.user, registration, confirmed_root=request.POST.get("confirmed_root"))
        return redirect(_storage_path(project))
    task = registration.task
    expired = task.expires_at <= timezone.now() and not registration.target_id
    return render(request, "broker_storage/target_registration.html", {"project": project, "registration": registration,
        "task": task, "expired": expired, "waiting": not expired and task.state in {"queued", "running"},
        "resolved_root": services.confirmed_directory_root(registration) if task.state == "succeeded" else None,
        "retry_url": _storage_path(project, "targets/new/?") + urlencode({"connection": str(registration.connection_id),
            "root_path": registration.root, "name": registration.name, "roles": registration.roles}, doseq=True)})


@human_view
def project_storage(request, project_id):
    project = managed_project(request, project_id)
    connections = project.connections.filter(enabled=True, archived=False).order_by("name", "id")
    selected = request.POST.get("connection_id") if request.method == "POST" else request.GET.get("connection")
    connection = None
    if selected:
        try:
            connection = get_object_or_404(connections, id=UUID(selected))
        except (ValueError, TypeError, AttributeError):
            from django.http import Http404
            raise Http404("Cluster not found.") from None
    else:
        connection = connections.first()
    form = None
    if connection:
        budget = ProjectStorageBudget.objects.filter(project=project).first()
        binding = ProjectStorageBinding.objects.filter(project=project, connection=connection).first()
        initial = {"budget_gib": Decimal(budget.bytes_budget) / GIB if budget and budget.bytes_budget is not None else None,
            "inodes_budget": budget.inodes_budget if budget else None,
            "retained_gib_estimate": (Decimal(budget.retained_bytes_estimate) / GIB).quantize(Decimal(".001")) if budget else 0,
            "retained_inodes_estimate": budget.retained_inodes_estimate if budget else 0,
            **{role: getattr(binding, f"{role}_id") if binding else None for role in ROLES}}
        # Form precision matches display, while the ledger continues to use bytes.
        if initial["budget_gib"] is not None:
            initial["budget_gib"] = initial["budget_gib"].quantize(Decimal(".001"))
        form = ProjectStorageForm(request.POST if request.method == "POST" else None, project=project,
                                   connection=connection, initial=initial)
        if request.method == "POST" and form.is_valid():
            services.save_project_storage(request.user, project, connection, form.cleaned_data)
            return redirect(f"/projects/{project.id}/storage/?connection={connection.id}")
    snapshot = services.inventory(project)
    assigned = {item.id: item for item in project.connections.select_related("worker").order_by("name", "id")}
    groups = {identity: {"connection": item, "assigned": True,
        "can_add": item.owner_id == request.user.id and item.enabled and not item.archived, "targets": []}
        for identity, item in assigned.items()}
    targets = list(StorageTarget.objects.filter(project=project).select_related("connection__worker", "pool", "owner")
        .order_by("connection__name", "name", "id"))
    personal_ids = set(StorageTargetRegistration.objects.filter(target__in=targets).values_list("target_id", flat=True))
    for target in targets:
        group = groups.setdefault(target.connection_id, {"connection": target.connection, "assigned": False,
            "can_add": False, "targets": []})
        group["targets"].append({"target": target, "current": services.target_current(target),
            "personal": target.id in personal_ids})
    return render(request, "broker_storage/project_storage.html", {"project": project, "connections": connections,
        "selected_connection": connection, "form": form, "snapshot": snapshot, "snapshot_text": json.dumps(snapshot, indent=2),
        "target_groups": list(groups.values()),
        "can_add": _setup_connections(request.user, project).exists()})


@boundary("GET")
def storage_inventory(request, project_id):
    credential, project = scope(request, "jobs:read", project_id=project_id)
    response = services.inventory(project, credential=credential)
    from django.http import JsonResponse
    result = JsonResponse(response)
    result["Cache-Control"] = "no-store, private"
    return result


@human_view
def pool_probes(request, pool_id, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = _project_context(request, project_id)
    pool = get_object_or_404(StorageCapacityPool.objects.filter(id=pool_id, owner=request.user,
        storagetarget__project=project).distinct())
    value = request.POST.get("enabled")
    if value not in {"true", "false"}:
        from compute_relay.control.errors import BrokerError
        raise BrokerError("INVALID_STORAGE", "Choose whether capacity checks are enabled.")
    services.set_probe_enabled(request.user, pool, value == "true")
    return redirect(_storage_path(project))


@boundary("POST")
def capacity_report(request, pool_id):
    return services.report_probe(worker_identity(request), pool_id, body(request, maximum=16384))


@boundary("POST")
def storage_plan(request, project_id):
    credential, _ = scope(request, "jobs:read", project_id=project_id)
    try:
        response = services.plan_for_credential(credential, body(request, maximum=16384))
    except StoragePlanningError as error:
        from compute_relay.control.errors import BrokerError
        raise BrokerError(error.code, error.message) from None
    from django.http import JsonResponse
    result = JsonResponse(response)
    result["Cache-Control"] = "no-store, private"
    return result
