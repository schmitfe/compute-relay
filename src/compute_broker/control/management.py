"""Human-only management. Issued project secrets exist only in the creation response."""

import functools
import json
from decimal import Decimal, ROUND_CEILING
from datetime import datetime
from urllib.parse import urlencode
from uuid import UUID

from django.db import transaction
from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect

from compute_broker.domain.validation import validate
from . import services
from .auth import human_permission, new_token, token_hash
from .errors import BrokerError
from .forms import (ALL_SCOPES, ClusterForm, EnvironmentForm, KeyForm, ProjectForm,
                    owned_connections, owned_environments, owned_workers)
from .models import (Attempt, Connection, Environment, Grant, IdempotencyRecord, Job, Project,
                     ProjectCredential)


def human_view(function):
    @csrf_protect
    @functools.wraps(function)
    def wrapped(request, *args, **kwargs):
        if request.method not in {"GET", "POST"}:
            return HttpResponseNotAllowed(["GET", "POST"])
        if not request.user.is_authenticated:
            return redirect("/login/") if request.method == "GET" else HttpResponse("A human session is required.", status=403)
        try:
            response = function(request, *args, **kwargs)
        except BrokerError as exc:
            response = render(request, "broker/management_error.html", {"error": exc.message, "code": exc.code}, status=exc.status)
        response["Cache-Control"] = "no-store, private"
        response["Pragma"] = "no-cache"
        response["Referrer-Policy"] = "same-origin"
        return response
    return wrapped


def managed_project(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    human_permission(request.user, project, "project_admin")
    return project


def _key_payload(data):
    return {"name": data["name"], "expires_at": data["expires_at"].isoformat(), "scopes": sorted(data["scopes"]),
            "execution_mode": data.get("execution_mode", "inherit"),
            "allowed_connection_ids": None if data["cluster_access"] == "inherit" else sorted(str(c.id) for c in data["allowed_connections"])}


def _one_time_record(project, actor, operation, nonce, payload, credential_id):
    try:
        key = str(UUID(str(nonce)))
    except (ValueError, TypeError):
        raise BrokerError("INVALID_SUBMISSION", "Open the form again before submitting.") from None
    operation = f"manage:{operation}:{actor.id}"
    if IdempotencyRecord.objects.filter(project=project, operation=operation, key=key).exists():
        raise BrokerError("SECRET_ALREADY_SHOWN", "This creation request was already processed. Keys cannot be retrieved; rotate the key if its value was lost.", 409)
    IdempotencyRecord.objects.create(project=project, operation=operation, key=key, request_sha256=services.digest(payload),
                                      response={"credential_id": str(credential_id)}, status=201)


def issue_key(project, user, data):
    human_permission(user, project, "viewer")
    for scope in data["scopes"]:
        human_permission(user, project, "viewer" if scope in {"jobs:read", "artifacts:read"} else "submitter")
    payload = _key_payload(data)
    with transaction.atomic():
        Project.objects.select_for_update().get(id=project.id)
        secret = new_token()
        credential = ProjectCredential(project=project, issued_by=user, name=payload["name"], token_hash=token_hash(secret), scopes=payload["scopes"],
                                       expires_at=data["expires_at"], allowed_connection_ids=payload["allowed_connection_ids"], execution_mode=payload["execution_mode"])
        # The display identifier is independent of the secret. No plaintext prefix is stored.
        credential.prefix = str(credential.id)[:12]
        _one_time_record(project, user, "key:create", data["submission_id"], payload, credential.id)
        credential.save()
        services.audit(project, user.id, "credential.created", credential.id)
    return credential, secret


def rotate_key(project, credential, user, nonce):
    human_permission(user, project, "viewer")
    with transaction.atomic():
        Project.objects.select_for_update().get(id=project.id)
        old = ProjectCredential.objects.select_for_update().get(id=credential.id, project=project)
        if old.issued_by_id != user.id:
            raise BrokerError("KEY_OWNER_REQUIRED", "Only the key's issuing user may rotate and receive its replacement.", 403)
        if old.revoked:
            raise BrokerError("KEY_REVOKED", "This key is already revoked. Create a new key instead.", 409)
        if old.expires_at <= timezone.now():
            raise BrokerError("KEY_EXPIRED", "This key has expired. Create a new key with a future expiry.", 409)
        secret = new_token()
        replacement = ProjectCredential(project=project, issued_by=old.issued_by, name=old.name, token_hash=token_hash(secret), scopes=list(old.scopes),
                                         expires_at=old.expires_at, allowed_connection_ids=old.allowed_connection_ids, execution_mode=old.execution_mode)
        replacement.prefix = str(replacement.id)[:12]
        _one_time_record(project, user, f"key:rotate:{old.id}", nonce, {"old_id": str(old.id)}, replacement.id)
        replacement.save()
        old.revoked, old.replaced_by = True, replacement
        old.save(update_fields=["revoked", "replaced_by"])
        services.audit(project, user.id, "credential.rotated", old.id, {"replacement_id": str(replacement.id)})
    return replacement, secret


def project_policy(project, data, revision):
    return {"schema_version": "1.0", "project_id": str(project.id), "revision": revision, "mode": data["mode"],
            "allowed_connection_ids": sorted(str(c.id) for c in data["connections"]),
            "allowed_environment_revision_sha256": sorted(set(e.revision_sha256 for e in data["environments"])),
            "allowed_gpu_types": data["gpu_types"], "max_active_jobs": data["max_active_jobs"], "max_total_jobs": data.get("max_total_jobs", 1000),
            "per_job_limits": {"cpu_cores": data["max_cpus"], "gpu_devices": data["max_gpus"], "memory_mib": data["memory_mib"],
                               "time_limit_seconds": data["wall_minutes"] * 60, "maximum_charge_duration_seconds": data["charge_seconds"]},
            "allowance": {"cpu_core_seconds": data["cpu_seconds"], "gpu_device_seconds": data["gpu_seconds"]},
            "admission_expires_at": data["expires_at"].isoformat()}


def save_project(user, data, project=None):
    with transaction.atomic():
        if project is None:
            project = Project.objects.create(name=data["name"], owner=user, paused=data["paused"])
            grant = None
        else:
            human_permission(user, project, "project_admin")
            project = Project.objects.select_for_update().get(id=project.id)
            grant = Grant.objects.select_for_update().get(project=project)
            if data["cpu_seconds"] < grant.spent_cpu + grant.reserved_cpu or data["gpu_seconds"] < grant.spent_gpu + grant.reserved_gpu:
                raise BrokerError("LIMIT_BELOW_COMMITTED", "The allowance cannot be lower than spent plus reserved compute. Cancel pending jobs or raise the total.", 409)
            if data.get("max_total_jobs", 1000) < Attempt.objects.filter(job__project=project).count():
                raise BrokerError("LIMIT_BELOW_COMMITTED", "The submission limit cannot be lower than the number of execution authorizations already used.", 409)
        policy = project_policy(project, data, grant.policy["revision"] + 1 if grant else 1)
        validate("project-policy.schema.json", policy)
        project.name, project.paused = data["name"], data["paused"]
        project.save(update_fields=["name", "paused"])
        project.connections.set(data["connections"])
        project.environments.set(data["environments"])
        if grant:
            grant.policy = policy
            grant.save(update_fields=["policy"])
        else:
            Grant.objects.create(project=project, policy=policy)
        services.audit(project, user.id, "project.policy_updated", project.id, {"revision": policy["revision"]})
    return project


def project_initial(project):
    policy = Grant.objects.get(project=project).policy
    limit = policy["per_job_limits"]
    return {"name": project.name, "mode": policy["mode"], "cpu_hours": (Decimal(policy["allowance"]["cpu_core_seconds"]) / 3600).quantize(Decimal(".000001"), rounding=ROUND_CEILING),
            "gpu_hours": (Decimal(policy["allowance"]["gpu_device_seconds"]) / 3600).quantize(Decimal(".000001"), rounding=ROUND_CEILING),
            "max_cpus": limit["cpu_cores"], "max_gpus": limit["gpu_devices"], "memory_mib": limit["memory_mib"],
            "wall_minutes": limit["time_limit_seconds"] // 60,
            "charge_minutes": (Decimal(limit["maximum_charge_duration_seconds"]) / 60).quantize(Decimal(".001"), rounding=ROUND_CEILING),
            "max_active_jobs": policy["max_active_jobs"], "max_total_jobs": policy.get("max_total_jobs", 1000),
            "expires_at": datetime.fromisoformat(policy["admission_expires_at"].replace("Z", "+00:00")),
            "connections": project.connections.all(), "environments": project.environments.all(),
            "allowed_gpu_types": ", ".join(policy["allowed_gpu_types"]), "paused": project.paused}


@human_view
def project_new(request):
    form = ProjectForm(request.POST if request.method == "POST" else None, user=request.user,
                       initial={"cpu_hours": 2, "gpu_hours": 0, "mode": "approval_required"})
    if request.method == "POST" and form.is_valid():
        project = save_project(request.user, form.cleaned_data)
        return redirect(f"/projects/{project.id}/")
    return render(request, "broker/project_form.html", {"form": form, "creating": True})


@human_view
def project_detail(request, project_id):
    project = managed_project(request, project_id)
    form = ProjectForm(request.POST if request.method == "POST" else None, user=request.user, initial=project_initial(project))
    error = ""
    if request.method == "POST" and form.is_valid():
        try:
            save_project(request.user, form.cleaned_data, project)
            return redirect(f"/projects/{project.id}/")
        except BrokerError as exc:
            error = exc.message
    from uuid import uuid4
    return render(request, "broker/project_detail.html", {"project": project, "form": form, "error": error,
        "keys": project.projectcredential_set.order_by("-created_at"), "allowance": services.allowance(project),
        "jobs": Job.objects.with_summary().filter(project=project).order_by("-created_at", "-id")[:100],
        "rotation_nonce": str(uuid4())})


@human_view
def key_new(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    human_permission(request.user, project, "viewer")
    form = KeyForm(request.POST if request.method == "POST" else None, project=project)
    if request.method == "POST" and form.is_valid():
        credential, secret = issue_key(project, request.user, form.cleaned_data)
        return render(request, "broker/key_created.html", {"credential": credential, "secret": secret, "project": project}, status=201)
    return render(request, "broker/key_form.html", {"form": form, "project": project, "creating": True})


@human_view
def key_rotate(request, project_id, key_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = get_object_or_404(Project, id=project_id)
    human_permission(request.user, project, "viewer")
    credential = get_object_or_404(ProjectCredential, id=key_id, project=project)
    credential, secret = rotate_key(project, credential, request.user, request.POST.get("submission_id"))
    return render(request, "broker/key_created.html", {"credential": credential, "secret": secret, "project": project, "rotated": True}, status=201)


@human_view
def key_revoke(request, project_id, key_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = managed_project(request, project_id)
    with transaction.atomic():
        Project.objects.select_for_update().get(id=project.id)
        credential = get_object_or_404(ProjectCredential.objects.select_for_update(), id=key_id, project=project)
        credential.revoked = True
        credential.save(update_fields=["revoked"])
        services.audit(project, request.user.id, "credential.revoked", credential.id)
    return redirect(f"/projects/{project.id}/")


@human_view
def key_edit(request, project_id, key_id):
    project = managed_project(request, project_id)
    credential = get_object_or_404(ProjectCredential, id=key_id, project=project)
    if credential.revoked:
        raise BrokerError("KEY_REVOKED", "Revoked keys cannot be reactivated.", 409)
    initial = {"name": credential.name, "expires_at": credential.expires_at, "scopes": credential.scopes,
               "execution_mode": credential.execution_mode,
               "cluster_access": "inherit" if credential.allowed_connection_ids is None else "restrict",
               "allowed_connections": credential.allowed_connection_ids or []}
    form = KeyForm(request.POST if request.method == "POST" else None, project=project, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            Project.objects.select_for_update().get(id=project.id)
            credential = ProjectCredential.objects.select_for_update().get(id=credential.id)
            if credential.revoked:
                raise BrokerError("KEY_REVOKED", "Revoked keys cannot be reactivated.", 409)
            for key, value in _key_payload(data).items():
                setattr(credential, key, data["expires_at"] if key == "expires_at" else value)
            credential.save(update_fields=["name", "expires_at", "scopes", "allowed_connection_ids", "execution_mode"])
            services.audit(project, request.user.id, "credential.permissions_updated", credential.id)
        return redirect(f"/projects/{project.id}/")
    return render(request, "broker/key_form.html", {"form": form, "project": project, "credential": credential})


@human_view
def clusters(request):
    from .cluster_setup import owned_registrations
    return render(request, "broker/clusters.html", {"clusters": owned_connections(request.user),
        "registrations": owned_registrations(request.user).filter(connection__isnull=True).order_by("created_at"),
        "workers": owned_workers(request.user)})


@human_view
def cluster_new(request):
    from .cluster_setup import RegistrationForm
    from .models import ClusterRegistration
    # Preserve the authenticated, scope-checked one-step API used by existing
    # operator tooling. The ordinary browser flow never requires a worker here.
    legacy = request.method == "POST" and ("worker" in request.POST or "credential_profile" in request.POST)
    if not legacy:
        form = RegistrationForm(request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            registration = ClusterRegistration.objects.create(owner=request.user, name=data["name"], backend=data["backend"],
                host=data["host"], remote_user=data["user"], port=data["port"], requested_root=data["remote_root"],
                connector_settings=data["connector_settings"])
            services.audit(None, request.user.id, "cluster.registered", registration.id)
            return redirect(f"/clusters/setup/{registration.id}/")
        return render(request, "broker/cluster_form.html", {"form": form})
    form = ClusterForm(request.POST if request.method == "POST" else None, user=request.user)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        profile = data["profile"]
        config = {"backend": profile["backend"], "credential_profile": data["credential_profile"],
                  "ssh": {"host": data["host"], "user": data["user"], "port": data["port"]}, "remote_root": data["remote_root"]}
        connection = Connection.objects.create(owner=request.user, worker=data["worker"], name=data["name"], backend=profile["backend"],
                                                configuration=config, revision_sha256=services.digest(config), setup_complete=False,
                                                auth_state="discovery_required", discovery_state="queued")
        services.audit(None, request.user.id, "connection.enrolled", connection.id)
        return redirect(f"/clusters/{connection.id}/")
    return render(request, "broker/cluster_form.html", {"form": form, "workers": owned_workers(request.user)})


@human_view
def cluster_detail(request, connection_id):
    from .models import ClusterRegistration
    connection = get_object_or_404(owned_connections(request.user), id=connection_id)
    return render(request, "broker/cluster_detail.html", {"cluster": connection,
        "personal_connector_allowed": connection.backend == "slurm" and connection.owner_id == request.user.id,
        "personal_setup": ClusterRegistration.objects.filter(owner=request.user, connection=connection).first(),
        "snapshot_text": json.dumps(connection.discovery_snapshot, indent=2),
        "software": connection.discovery_snapshot.get("software", {}), "profile": connection.configuration.get("site_profile", {}),
        "environments": owned_environments(request.user).filter(connection=connection).order_by("name")})


@human_view
def cluster_discover(request, connection_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    with transaction.atomic():
        connection = get_object_or_404(owned_connections(request.user).select_for_update(), id=connection_id)
        if not connection.configuration.get("credential_profile"):
            raise BrokerError("PROFILE_REQUIRED", "Enroll this cluster with an approved worker credential profile before requesting discovery.", 409)
        connection.discovery_state, connection.discovery_error = "queued", ""
        connection.discovery_lease_id, connection.discovery_lease_expires_at = None, None
        connection.discovery_generation += 1
        connection.save(update_fields=["discovery_state", "discovery_error", "discovery_lease_id", "discovery_lease_expires_at", "discovery_generation"])
        services.audit(None, request.user.id, "connection.discovery_requested", connection.id)
    return redirect(f"/clusters/{connection.id}/")


@human_view
def cluster_toggle(request, connection_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    with transaction.atomic():
        connection = get_object_or_404(owned_connections(request.user).select_for_update(), id=connection_id)
        connection.enabled = request.POST.get("enabled") == "true"
        connection.save(update_fields=["enabled"])
        services.audit(None, request.user.id, "connection.enabled" if connection.enabled else "connection.disabled", connection.id)
    return redirect(f"/clusters/{connection.id}/")


def _legacy_environment_redirect(request, *, creating=False):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    value = request.GET.get("connection")
    if not value:
        return redirect("/clusters/")
    try:
        identity = UUID(value)
    except (ValueError, TypeError):
        raise BrokerError("INVALID_CONNECTION", "Choose a valid cluster.") from None
    connection = get_object_or_404(owned_connections(request.user), id=identity)
    location = f"/clusters/{connection.id}/environments/" + ("new/" if creating else "")
    if creating:
        # Carry only supported recipe suggestions, never arbitrary query data.
        query = urlencode([(key, request.GET[key]) for key in ("kind", "module", "interpreter", "prefix") if key in request.GET])
        if query:
            location += "?" + query
    return redirect(location)


@human_view
def environments(request, connection_id=None):
    if connection_id is None:
        return _legacy_environment_redirect(request)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    connection = get_object_or_404(owned_connections(request.user), id=connection_id)
    recipes = owned_environments(request.user).filter(connection=connection).order_by("name", "id")
    return render(request, "broker/environments.html", {"cluster": connection, "selected_cluster": connection, "environments": recipes})


@human_view
def environment_new(request, connection_id=None):
    if connection_id is None:
        return _legacy_environment_redirect(request, creating=True)
    connection = get_object_or_404(owned_connections(request.user), id=connection_id)
    initial = {"connection": str(connection.id), "kind": request.GET.get("kind", "system"),
               "interpreter": request.GET.get("interpreter", "/usr/bin/python3"), "prefix": request.GET.get("prefix", "")}
    software = connection.discovery_snapshot.get("software", {})
    if initial["kind"] == "modules":
        module_data = software.get("modules", {})
        initial.update(interpreter="python3", module_init=module_data.get("init_path", ""),
                       module_paths="\n".join(module_data.get("module_paths", [])))
    elif initial["kind"] == "conda":
        initial["conda_executable"] = software.get("conda", {}).get("executable", "")
    if request.GET.get("module"):
        initial["discovered_modules"] = [request.GET["module"]]
    bound = None
    target_conflict = False
    if request.method == "POST":
        bound = request.POST.copy()
        supplied = bound.getlist("connection")
        try:
            target_conflict = bool(supplied) and (len(supplied) != 1 or UUID(supplied[0]) != connection.id)
        except (ValueError, TypeError, AttributeError):
            target_conflict = True
        bound["connection"] = str(connection.id)
    form = EnvironmentForm(bound, user=request.user, initial=initial)
    form.fields["connection"].queryset = owned_connections(request.user).filter(id=connection.id)
    valid = form.is_valid() if request.method == "POST" else False
    if target_conflict:
        form.add_error("connection", "This recipe belongs to the cluster in the page address. Open the other cluster to create its recipe.")
        valid = False
    if valid:
        data = form.cleaned_data
        config = {"kind": data["kind"], "interpreter": data["interpreter"], "modules": data["module_names"]}
        for key in ("module_init", "prefix", "conda_executable"):
            if data.get(key):
                config[key] = data[key]
        if data["module_path_list"]:
            config["module_paths"] = data["module_path_list"]
        from compute_broker.backends import validate_environment_config
        try:
            validate_environment_config(config)
        except ValueError:
            form.add_error(None, "This environment profile is not supported. Check its interpreter, prefix and module settings.")
        else:
            environment = Environment.objects.create(owner=request.user, connection=connection, name=data["name"], configuration=config, revision_sha256=services.digest(config))
            services.audit(None, request.user.id, "environment.created", environment.id)
            return redirect(f"/clusters/{connection.id}/environments/")
    return render(request, "broker/environment_form.html", {"form": form, "cluster": connection, "selected_cluster": connection})
