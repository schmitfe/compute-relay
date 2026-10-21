"""Human configuration and project bearer dry-runs; no data-plane operations."""

import json
from decimal import Decimal
from uuid import UUID

from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from compute_broker.control.forms import owned_connections
from compute_broker.control.management import human_view, managed_project
from compute_broker.control.views import body, boundary, scope
from . import services
from .forms import GIB, ProjectStorageForm, StorageTargetForm
from .models import ProjectStorageBinding, ProjectStorageBudget, StorageTarget
from .planning import ROLES, StoragePlanningError


@human_view
def targets(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    rows = StorageTarget.objects.select_related("connection__worker", "pool")
    if not request.user.is_superuser:
        rows = rows.filter(owner=request.user)
    return render(request, "broker_storage/targets.html", {"targets": [{"target": target, "current": services.target_current(target)}
                                                                     for target in rows.order_by("connection__name", "name")]})


@human_view
def target_new(request):
    form = StorageTargetForm(request.POST if request.method == "POST" else None, user=request.user,
                             initial={"connection": request.GET.get("connection")})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        services.register_target(request.user, data["connection"], data["root_alias"], data["name"], pool=data.get("pool"))
        return redirect("/storage/targets/")
    return render(request, "broker_storage/target_form.html", {"form": form, "cluster": form.selected_connection,
        "clusters": owned_connections(request.user).filter(enabled=True).order_by("name")})


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
