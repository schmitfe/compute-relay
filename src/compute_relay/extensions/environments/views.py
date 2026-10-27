"""Extension-only API and human policy/promotion pages."""

from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_protect

from compute_relay.control.auth import connection_allowed, human_permission, require_connection
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Project
from compute_relay.control.views import boundary, body, idem, scope

from . import services
from .forms import BuildPolicyForm
from .models import EnvironmentBuild, EnvironmentBuildPolicy


@boundary("GET")
def inventory(request, environment_id):
    from . import inspection
    credential, _ = scope(request, "jobs:read")
    environment = inspection.environment_for(credential, environment_id)
    latest = inspection.latest(environment, credential.project)
    pending = inspection.EnvironmentInspection.objects.filter(environment=environment, project=credential.project).order_by("-created_at", "-id").first()
    build = EnvironmentBuild.objects.filter(environment=environment, spec__project=credential.project).first()
    runtime = latest.observation.get("runtime") if latest else build.receipt.get("receipt", {}).get("runtime") if build else None
    return {"environment_id": str(environment.id), "registered_name": environment.name,
        "registered_revision_sha256": environment.revision_sha256, "summary": services.environment_summary(environment, credential.project),
        "runtime": runtime, "latest_inspection": inspection.serialize(pending) if pending else None,
        "supported_languages": ["python"]}


@boundary("POST")
def inspect(request, environment_id):
    from . import inspection
    credential, _ = scope(request, "jobs:write")
    scope(request, "jobs:execute")
    environment = inspection.environment_for(credential, environment_id)
    data = body(request)
    if set(data) - {"resources", "slurm"}:
        raise ValueError("Invalid inspection options")
    created, _ = idem(request, credential, "environment:inspect:" + str(environment.id), data,
        lambda: inspection.create(credential, environment))
    current = inspection.EnvironmentInspection.objects.get(id=created["id"], credential=credential, project=credential.project)
    inspection.prepare(current, data.get("resources"), data.get("slurm"))
    execution = inspection.request_execution(current, credential)
    current.refresh_from_db()
    return {**inspection.serialize(current), "execution": execution}


@boundary("POST")
def packages(request, environment_id):
    from .inspection import environment_for
    from .requests import package_recipe
    credential, _ = scope(request, "environments:build")
    environment = environment_for(credential, environment_id)
    data = body(request)
    if "packages" not in data or set(data) - {"packages", "name", "resources", "slurm"}:
        raise ValueError("Invalid package request")
    # Bind idempotency to the caller's original request, before resolving mutable
    # parent/catalog metadata. The new environment always has a unique prefix.
    created, _ = idem(request, credential, "environment:packages:" + str(environment.id), data,
        lambda: services.create_build(credential, package_recipe(credential, environment, data)))
    build = EnvironmentBuild.objects.get(id=created["id"])
    if created.get("cache_hit"):
        return {"build": services.serialize(build, cache_hit=True), "execution": None,
            "updates_replace_existing_environment": False}
    services.prepare_build(build, credential)
    build.refresh_from_db()
    execution, _ = idem(request, credential, "environment:run:" + str(build.id), {}, lambda: services.request_build(build, credential))
    build.refresh_from_db()
    return {"build": services.serialize(build), "execution": execution, "updates_replace_existing_environment": False}


def _build(request, build_id, permission):
    credential, project = scope(request, permission)
    build = EnvironmentBuild.objects.select_related("spec__project", "spec__connection", "policy").get(id=build_id, spec__project=project)
    require_connection(credential, build.spec.connection_id)
    return credential, build


@boundary("GET", "POST")
def builds(request, project_id):
    credential, project = scope(request, "environments:build" if request.method == "POST" else "jobs:read", project_id=project_id)
    if request.method == "GET":
        return {"builds": [services.serialize(build) for build in EnvironmentBuild.objects.filter(spec__project=project).order_by("-created_at")[:100]
                           if connection_allowed(credential, build.spec.connection_id)],
                "supported_managers": ["venv"], "unavailable_managers": ["conda", "micromamba", "poetry"]}
    document = body(request)
    return idem(request, credential, "environments:create", document, lambda: services.create_build(credential, document))


@boundary("GET")
def detail(request, build_id):
    _, build = _build(request, build_id, "jobs:read")
    return services.serialize(build)


@boundary("POST")
def prepare(request, build_id):
    credential, build = _build(request, build_id, "environments:build")
    document = body(request)
    if document:
        raise BrokerError("INVALID_REQUEST", "Preparation accepts an empty object.")
    # Core preparation atomically pins the package; file quota reservations must
    # be committed outside the idempotency transaction.
    key = request.headers.get("Idempotency-Key", "")
    if not 1 <= len(key) <= 128:
        raise BrokerError("IDEMPOTENCY_REQUIRED", "Supply an Idempotency-Key.")
    result = services.prepare_build(build, credential)
    return idem(request, credential, f"environment:{build_id}:prepare", document, lambda: result)


@boundary("POST")
def execute(request, build_id):
    credential, build = _build(request, build_id, "jobs:execute")
    document = body(request)
    if document:
        raise BrokerError("INVALID_REQUEST", "Execution accepts an empty object.")
    return idem(request, credential, f"environment:{build_id}:execute", document, lambda: services.request_build(build, credential))


@boundary("GET", "PUT", exempt=False)
def policy_api(request, project_id):
    if request.method == "GET":
        credential, project = scope(request, "jobs:read", project_id=project_id)
        policy = EnvironmentBuildPolicy.objects.filter(project=project).first()
        bases = project.environments.filter(id__in=policy.configuration.get("allowed_base_environment_ids", []) if policy else [])
        return {"enabled": bool(policy and policy.configuration.get("enabled")),
                "revision": policy.revision if policy else None, "configuration": policy.configuration if policy else {},
                "supported_managers": ["venv"],
                "bootstrap_environments": [{"id": str(base.id), "revision_sha256": base.revision_sha256,
                    "name": base.name, "connection_id": str(base.connection_id), "configuration": base.configuration}
                    for base in bases if connection_allowed(credential, base.connection_id)],
                "storage_assurance": "advisory; installations run only in scheduled jobs"}
    if not request.user.is_authenticated:
        raise BrokerError("HUMAN_REQUIRED", "A human administrator session is required.", 403)
    project = Project.objects.get(id=project_id)
    human_permission(request.user, project, "project_admin")
    policy = services.update_policy(project, request.user, body(request))
    return {"revision": policy.revision, "configuration": policy.configuration}


@csrf_protect
def portal(request, project_id):
    if not request.user.is_authenticated:
        return redirect("/login/")
    project = Project.objects.get(id=project_id)
    try:
        human_permission(request.user, project, "viewer")
    except BrokerError:
        return HttpResponse("Project access is required.", status=403)
    if request.method != "GET":
        return HttpResponse(status=405)
    return render(request, "broker_environments/builds.html", {"project": project,
        "builds": EnvironmentBuild.objects.filter(spec__project=project).select_related("spec", "job", "environment").order_by("-created_at")[:100]})


@csrf_protect
def policy_portal(request, project_id):
    if not request.user.is_authenticated:
        return redirect("/login/")
    project = Project.objects.get(id=project_id)
    try:
        human_permission(request.user, project, "project_admin")
    except BrokerError:
        return HttpResponse("Project administration is required.", status=403)
    if request.method not in {"GET", "POST"}:
        return HttpResponse(status=405)
    policy = EnvironmentBuildPolicy.objects.filter(project=project).first()
    form = BuildPolicyForm(request.POST if request.method == "POST" else None, project=project,
                           configuration=policy.configuration if policy else None)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_policy(project, request.user, form.cleaned_data["configuration"])
        except BrokerError as exc:
            form.add_error(None, exc.message)
        else:
            return redirect(f"/projects/{project.id}/environment-builds/")
    return render(request, "broker_environments/policy.html", {"project": project, "form": form})


@boundary("POST", exempt=False)
def promote(request, build_id):
    if not request.user.is_authenticated:
        raise BrokerError("HUMAN_REQUIRED", "A human administrator session is required.", 403)
    build = EnvironmentBuild.objects.select_related("spec__project", "environment").get(id=build_id)
    response = services.promote(build, request.user)
    return redirect(f"/projects/{build.spec.project_id}/environment-builds/") if request.content_type != "application/json" else response
