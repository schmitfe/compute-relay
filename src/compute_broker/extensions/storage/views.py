"""Human configuration and project bearer dry-runs; no data-plane operations."""

import json
from decimal import Decimal
from uuid import UUID
from urllib.parse import urlencode

from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from compute_broker.control.forms import owned_connections
from compute_broker.control.auth import worker_identity
from compute_broker.control.management import human_view, managed_project
from compute_broker.control.views import body, boundary, scope
from . import services
from .forms import GIB, ProjectStorageForm, StorageTargetForm
from .models import (ProjectStorageBinding, ProjectStorageBudget, StorageCapacityPool,
                     StorageTarget, StorageTargetRegistration)
from .planning import ROLES, StoragePlanningError


@human_view
def targets(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    rows = StorageTarget.objects.select_related("connection__worker", "pool")
    if not request.user.is_superuser:
        rows = rows.filter(owner=request.user)
    return render(request, "broker_storage/targets.html", {"targets": [{"target": target, "current": services.target_current(target),
        "personal": StorageTargetRegistration.objects.filter(target=target).exists()}
                                                                     for target in rows.order_by("connection__name", "name")]})


@human_view
def target_new(request):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    initial = {"connection": request.GET.get("connection"), "root_path": request.GET.get("root_path", ""),
        "name": request.GET.get("name", ""), "pool": request.GET.get("pool") or None,
        "enable_probes": request.GET.get("enable_probes") == "true"}
    if request.GET.getlist("roles"):
        initial["roles"] = request.GET.getlist("roles")
    form = StorageTargetForm(request.POST if request.method == "POST" else None, user=request.user,
                             initial=initial)
    if request.method == "POST" and request.POST.get("action") == "browse_directory" and form.personal:
        return redirect(f"/storage/targets/directories/{form.selected_connection.id}/?" +
                        urlencode(_retained_selection(request.POST), doseq=True))
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if form.personal:
            registration = services.queue_personal_target(request.user, data["connection"], data["root_path"],
                data["name"], data["roles"], data["connection_revision"], data["submission_id"])
            return redirect(f"/storage/targets/registrations/{registration.id}/")
        services.register_target(request.user, data["connection"], data["root_alias"], data["name"],
                                 pool=data.get("pool"), enable_probes=data["enable_probes"])
        return redirect("/storage/targets/")
    return render(request, "broker_storage/target_form.html", {"form": form, "cluster": form.selected_connection,
        "clusters": owned_connections(request.user).filter(enabled=True).order_by("name")})


def _retained_selection(document):
    """Retain human form choices, never credentials or arbitrary return URLs."""
    return {"name": document.get("name", "")[:100], "pool": document.get("pool", "")[:40],
        "enable_probes": "true" if document.get("enable_probes") in {"true", "on"} else "false",
        "roles": [role for role in document.getlist("roles") if role in ROLES]}


@human_view
def target_directories(request, connection_id):
    from compute_broker.control.connector_setup import enqueue, own_connection
    from compute_broker.control.cluster_setup import canonical_root
    from compute_broker.control.errors import BrokerError
    from compute_broker.control.models import ConnectorTask
    connection = own_connection(request.user, connection_id)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    selected = _retained_selection(request.POST if request.method == "POST" else request.GET)
    if request.method == "POST":
        path = request.POST.get("path", "")
        if path and not canonical_root(path) and path != "/":
            raise BrokerError("INVALID_DIRECTORY", "Choose a canonical absolute directory.")
        task = enqueue(request.user, connection.id, "browse", {"path": path}, request.POST.get("connection_revision"))
        return redirect(f"/storage/targets/directories/{connection.id}/?" +
                        urlencode({**selected, "task": str(task.id)}, doseq=True))
    task = None
    if request.GET.get("task"):
        try:
            identifier = UUID(request.GET["task"])
        except ValueError:
            raise BrokerError("INVALID_TASK", "Choose a valid directory request.") from None
        task = get_object_or_404(ConnectorTask, id=identifier, owner=request.user, connection=connection, kind="browse")
        from django.utils import timezone
        if task.expires_at <= timezone.now() and task.state in {"queued", "running"}:
            task.state, task.error_code = "failed", "CONNECTOR_TASK_EXPIRED"
    result = task.result if task and task.state == "succeeded" else {}
    select_url = "/storage/targets/new/?" + urlencode({**selected, "connection": str(connection.id),
        "root_path": result.get("path", "")}, doseq=True)
    return render(request, "broker_storage/target_directories.html", {"cluster": connection, "task": task,
        "result": result, "selection": selected, "select_url": select_url,
        "connector_ready": "personal_setup_v1" in connection.worker.connector_features,
        "waiting": bool(task and task.state in {"queued", "running"})})


@human_view
def target_registration(request, registration_id):
    from compute_broker.control.connector_setup import own_connection
    from django.utils import timezone
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    registration = get_object_or_404(StorageTargetRegistration.objects.select_related("task", "connection"),
        id=registration_id, owner=request.user)
    own_connection(request.user, registration.connection_id)
    if request.method == "POST":
        services.finish_personal_target(request.user, registration, confirmed_root=request.POST.get("confirmed_root"))
        return redirect("/storage/targets/")
    task = registration.task
    expired = task.expires_at <= timezone.now() and not registration.target_id
    return render(request, "broker_storage/target_registration.html", {"registration": registration,
        "task": task, "expired": expired, "waiting": not expired and task.state in {"queued", "running"},
        "resolved_root": services.confirmed_directory_root(registration) if task.state == "succeeded" else None,
        "retry_url": "/storage/targets/new/?" + urlencode({"connection": str(registration.connection_id),
            "root_path": registration.root, "name": registration.name, "roles": registration.roles}, doseq=True)})


@human_view
def project_storage(request, project_id):
    project = managed_project(request, project_id)
    connections = project.connections.filter(enabled=True).order_by("name", "id")
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
    return render(request, "broker_storage/project_storage.html", {"project": project, "connections": connections,
        "selected_connection": connection, "form": form, "snapshot": snapshot, "snapshot_text": json.dumps(snapshot, indent=2)})


@boundary("GET")
def storage_inventory(request, project_id):
    credential, project = scope(request, "jobs:read", project_id=project_id)
    response = services.inventory(project, credential=credential)
    from django.http import JsonResponse
    result = JsonResponse(response)
    result["Cache-Control"] = "no-store, private"
    return result


@human_view
def pool_probes(request, pool_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    pools = StorageCapacityPool.objects.all() if request.user.is_superuser else StorageCapacityPool.objects.filter(owner=request.user)
    pool = get_object_or_404(pools, id=pool_id)
    value = request.POST.get("enabled")
    if value not in {"true", "false"}:
        from compute_broker.control.errors import BrokerError
        raise BrokerError("INVALID_STORAGE", "Choose whether capacity checks are enabled.")
    services.set_probe_enabled(request.user, pool, value == "true")
    return redirect("/storage/targets/")


@boundary("POST")
def capacity_report(request, pool_id):
    return services.report_probe(worker_identity(request), pool_id, body(request, maximum=16384))


@boundary("POST")
def storage_plan(request, project_id):
    credential, _ = scope(request, "jobs:read", project_id=project_id)
    try:
        response = services.plan_for_credential(credential, body(request, maximum=16384))
    except StoragePlanningError as error:
        from compute_broker.control.errors import BrokerError
        raise BrokerError(error.code, error.message) from None
    from django.http import JsonResponse
    result = JsonResponse(response)
    result["Cache-Control"] = "no-store, private"
    return result
