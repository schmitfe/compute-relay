"""Human-owned post-login setup; agents cannot browse or change SSH scope."""

from datetime import timedelta
import json
import re
from urllib.parse import urlencode
from uuid import UUID, uuid4

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from compute_broker.domain.cluster_configuration import TIMING_FIELDS
from .auth import worker_identity
from .cluster_services import PUBLIC_CONFIG_FIELDS
from .cluster_setup import canonical_root
from .errors import BrokerError
from .identity.authority import cached_user_active
from .identity.models import AuthenticationIdentity
from .management import human_view
from .models import Attempt, ClusterRegistration, Connection, ConnectorEnrollment, ConnectorTask, Project, User, WorkerIdentity
from .services import audit, digest
from .views import body, boundary


def personal(connection):
    return ConnectorEnrollment.objects.filter(worker_id=connection.worker_id, consumed_at__isnull=False,
        registration__connection=connection).exists()


def own_connection(user, connection_id):
    if not cached_user_active(user):
        raise BrokerError("AUTH_REQUIRED", "A current human account is required.", 403)
    connection = get_object_or_404(Connection, id=connection_id, owner=user, archived=False)
    if not personal(connection):
        raise BrokerError("PERSONAL_CONNECTOR_REQUIRED", "This configuration needs a personal connector. Managed worker profiles are configured by their operator.", 409)
    return connection


def idle(connection):
    if Attempt.objects.filter(connection=connection).exclude(accounting_state__in=["settled", "released"]).exists():
        raise BrokerError("CONNECTION_BUSY", "Finish or reconcile existing executions before changing cluster configuration.", 409)


def locked_connection(reference, worker=None):
    # Match removal, re-pair and dispatch authority order. Updating Registration
    # after a Worker lock would deadlock with owner-driven removal/re-pair.
    owner = User.objects.select_for_update(no_key=True).get(id=reference.owner_id)
    AuthenticationIdentity.objects.select_for_update().filter(user=owner).first()
    if not cached_user_active(owner):
        raise BrokerError("AUTH_REQUIRED", "Account verification is required.", 403)
    registration = get_object_or_404(ClusterRegistration.objects.select_for_update(), connection=reference, owner=owner, archived=False)
    worker = WorkerIdentity.objects.select_for_update().get(id=worker.id if worker else reference.worker_id)
    if worker.revoked or worker.owner_id != reference.owner_id:
        raise BrokerError("AUTH_REQUIRED", "Connector access was withdrawn.", 403)
    list(Project.objects.select_for_update(of=("self",)).filter(connections=reference).order_by("id"))
    current = Connection.objects.select_for_update().get(id=reference.id, archived=False, worker=worker)
    return current, worker, registration


def expire_tasks(connection):
    ConnectorTask.objects.filter(connection=connection, state__in=["queued", "running"], expires_at__lte=timezone.now()).update(
        state="failed", error_code="CONNECTOR_TASK_EXPIRED")


def enqueue(user, connection_id, kind, payload, revision):
    reference = own_connection(user, connection_id)
    with transaction.atomic():
        owner = User.objects.select_for_update(no_key=True).get(id=user.id)
        if not cached_user_active(owner):
            raise BrokerError("AUTH_REQUIRED", "Account verification is required.", 403)
        current, worker, _registration = locked_connection(reference)
        if current.revision_sha256 != revision:
            raise BrokerError("REVISION_CHANGED", "The cluster changed. Reload this page before continuing.", 409)
        if "personal_setup_v1" not in worker.connector_features:
            raise BrokerError("CONNECTOR_UPGRADE_REQUIRED", "Restart your personal connector with the updated broker environment to enable interactive configuration.", 409)
        if kind == "mkdir" and "personal_folders_v1" not in worker.connector_features:
            raise BrokerError("CONNECTOR_UPGRADE_REQUIRED", "Restart your personal connector to enable the New folder action.", 409)
        if (kind == "configure" and payload.get("module_availability_enabled") is True
                and "personal_module_availability_v1" not in worker.connector_features):
            raise BrokerError("CONNECTOR_UPGRADE_REQUIRED", "Restart your personal connector to enable module avail discovery.", 409)
        expire_tasks(current)
        pending = ConnectorTask.objects.filter(connection=current, state__in=["queued", "running"])
        if pending.filter(kind="configure").exists() or pending.count() >= 4:
            raise BrokerError("CONFIGURATION_PENDING", "Wait for the current connector operation before trying again.", 409)
        if kind == "configure":
            if current.discovery_state != "ready" or not current.discovery_snapshot:
                raise BrokerError("DISCOVERY_REQUIRED", "Finish cluster discovery before confirming its configuration.", 409)
            idle(current)
            current.setup_complete = False
            current.configuration_confirmed = False
            current.save(update_fields=["setup_complete", "configuration_confirmed"])
        task = ConnectorTask.objects.create(connection=current, owner=owner, kind=kind, request=payload,
            connection_revision=current.revision_sha256, expires_at=timezone.now() + timedelta(minutes=5))
        audit(None, owner.id, "cluster." + kind + "_requested", task.id, {"connection_id": str(current.id)})
        return task


@human_view
def directories(request, connection_id):
    connection = own_connection(request.user, connection_id)
    if request.method == "POST":
        path = request.POST.get("path", "")
        if path and not canonical_root(path) and path != "/":
            raise BrokerError("INVALID_DIRECTORY", "Use a canonical absolute directory path without shell syntax.")
        action = request.POST.get("action", "browse")
        if action == "mkdir":
            name = request.POST.get("name", "")
            if (not path or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) or name in {".", ".."}
                    or len(path.rstrip("/") + "/" + name) > 240):
                raise BrokerError("INVALID_FOLDER_NAME", "Enter one folder name using letters, numbers, dots, underscores or hyphens, up to 80 characters.")
            payload = {"path": path, "name": name}
        elif action == "browse":
            payload = {"path": path}
        else:
            raise BrokerError("INVALID_ACTION", "Choose a supported directory action.")
        task = enqueue(request.user, connection_id, action, payload, request.POST.get("connection_revision"))
        return redirect(f"/clusters/{connection_id}/directories/?" + urlencode({"task": str(task.id)}))
    task = None
    if request.GET.get("task"):
        try:
            task_id = UUID(request.GET["task"])
        except ValueError:
            raise BrokerError("INVALID_TASK", "Choose a valid directory request.") from None
        task = get_object_or_404(ConnectorTask, id=task_id, connection=connection, owner=request.user, kind__in=["browse", "mkdir"])
        if task.expires_at <= timezone.now() and task.state in {"queued", "running"}:
            # Expiry is displayed without making a GET mutate the task.
            task.state, task.error_code = "failed", "CONNECTOR_TASK_EXPIRED"
    result = task.result if task and task.state == "succeeded" else {}
    return render(request, "broker/cluster_directories.html", {"cluster": connection, "task": task,
        "result": result, "waiting": bool(task and task.state in {"queued", "running"}),
        "connector_ready": "personal_setup_v1" in connection.worker.connector_features,
        "folders_ready": "personal_folders_v1" in connection.worker.connector_features})


@human_view
def configure(request, connection_id):
    from .cluster_configuration import ClusterConfigurationForm, derive_cluster_configuration
    connection = own_connection(request.user, connection_id)
    initial_root = request.GET.get("remote_root", connection.configuration.get("remote_root", ""))
    form = ClusterConfigurationForm(request.POST if request.method == "POST" else None,
        snapshot=connection.discovery_snapshot, current_profile=connection.configuration.get("site_profile", {}),
        initial={"module_availability_enabled": connection.registration.connector_settings.get("module_availability_enabled", False)})
    root_error = None
    if request.method == "POST":
        initial_root = request.POST.get("remote_root", "")
        if not canonical_root(initial_root):
            root_error = "Choose a canonical absolute working directory. Use Browse directories to select it on the cluster."
        if form.is_valid() and not root_error:
            task = enqueue(request.user, connection_id, "configure", {"remote_root": initial_root,
                "site_profile": form.site_profile(), "module_availability_enabled": form.cleaned_data["module_availability_enabled"]},
                request.POST.get("connection_revision"))
            return redirect(f"/clusters/{connection_id}/configuration/?" + urlencode({"task": str(task.id)}))
    task = None
    if request.GET.get("task"):
        try:
            task_id = UUID(request.GET["task"])
        except ValueError:
            raise BrokerError("INVALID_TASK", "Choose a valid configuration request.") from None
        task = get_object_or_404(ConnectorTask, id=task_id, owner=request.user, connection=connection, kind="configure")
        if request.method == "GET":
            initial_root = task.request["remote_root"]
            form = ClusterConfigurationForm(snapshot=connection.discovery_snapshot, current_profile=task.request["site_profile"],
                initial={"module_availability_enabled": task.request.get("module_availability_enabled", False)})
    return render(request, "broker/cluster_configuration.html", {"cluster": connection, "form": form,
        "remote_root": initial_root, "root_error": root_error, "task": task,
        "waiting": bool(task and task.state in {"queued", "running"} and task.expires_at > timezone.now()),
        "derived": derive_cluster_configuration(connection.discovery_snapshot, connection.configuration.get("site_profile", {})),
        "connector_ready": "personal_setup_v1" in connection.worker.connector_features})


def authenticated_connection(request, document):
    worker = worker_identity(request)
    connection = get_object_or_404(Connection, id=UUID(document["connection_id"]), worker=worker, archived=False)
    if not personal(connection) or not cached_user_active(connection.owner):
        raise BrokerError("AUTH_REQUIRED", "A current personal connector is required.", 403)
    return worker, connection


@boundary("POST")
def tasks(request):
    document = body(request, maximum=16 * 1024)
    worker, reference = authenticated_connection(request, document)
    with transaction.atomic():
        connection, worker, _registration = locked_connection(reference, worker)
        defaults_deferred = False
        if document.get("hello") is True:
            features = document.get("features", ["personal_setup_v1"])
            if not isinstance(features, list) or any(not isinstance(item, str) or item not in {"personal_setup_v1", "personal_folders_v1", "personal_module_availability_v1"} for item in features):
                raise BrokerError("INVALID_FEATURES", "Unsupported connector setup capabilities.")
            worker.connector_features = list(dict.fromkeys(features))
            worker.save(update_fields=["connector_features"])
            if document.get("needs_discovery") is True:
                if Attempt.objects.filter(connection=connection).exclude(accounting_state__in=["settled", "released"]).exists():
                    defaults_deferred = True
                else:
                    connection.setup_complete = False
                    connection.configuration_confirmed = False
                    connection.discovery_state = "queued"
                    connection.discovery_lease_id = connection.discovery_lease_expires_at = None
                    connection.discovery_generation += 1
                    connection.save()
        expire_tasks(connection)
        pending_configuration = None
        if document.get("pending_task_id"):
            pending_configuration = ConnectorTask.objects.filter(id=UUID(document["pending_task_id"]),
                connection=connection, owner_id=connection.owner_id, kind="configure").values("state", "error_code").first()
        now = timezone.now()
        task = ConnectorTask.objects.select_for_update().filter(connection=connection,
            state__in=["queued", "running"], expires_at__gt=now).filter(
                Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)).order_by("created_at").first()
        if task is None:
            return {"task": None, "defaults_deferred": defaults_deferred, "pending_configuration": pending_configuration}
        if task.connection_revision != connection.revision_sha256:
            task.state, task.error_code = "failed", "REVISION_CHANGED"
            task.save(update_fields=["state", "error_code"])
            return {"task": None}
        if task.kind == "configure":
            idle(connection)
        task.state, task.lease_id = "running", uuid4()
        task.generation += 1
        task.lease_expires_at = min(now + timedelta(seconds=120), task.expires_at)
        task.save(update_fields=["state", "lease_id", "generation", "lease_expires_at"])
        return {"task": {"id": str(task.id), "kind": task.kind, "request": task.request,
            "lease_id": str(task.lease_id), "generation": task.generation}, "pending_configuration": pending_configuration}


@boundary("POST")
def report(request, task_id):
    document = body(request, maximum=160 * 1024)
    worker, reference = authenticated_connection(request, document)
    with transaction.atomic():
        connection, worker, registration = locked_connection(reference, worker)
        task = get_object_or_404(ConnectorTask.objects.select_for_update(), id=task_id, connection=connection)
        if str(task.lease_id) != document.get("lease_id") or task.generation != document.get("generation"):
            raise BrokerError("STALE_LEASE", "The connector operation lease changed.", 409)
        if task.state == "succeeded":
            return {"accepted": True}
        if (task.state != "running" or task.lease_expires_at <= timezone.now() or task.expires_at <= timezone.now()
                or task.connection_revision != connection.revision_sha256):
            raise BrokerError("STALE_LEASE", "The connector operation expired or the configuration changed.", 409)
        result = document.get("result", {})
        if not isinstance(result, dict) or len(json.dumps(result).encode()) > 128 * 1024:
            raise BrokerError("INVALID_RESULT", "Invalid connector operation result.")
        if document.get("outcome") != "succeeded":
            code = document.get("error_code", "CONNECTOR_OPERATION_FAILED")
            task.state, task.error_code = "failed", code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", str(code)) else "CONNECTOR_OPERATION_FAILED"
        elif task.kind in {"browse", "mkdir"}:
            allowed = {"ok", "path", "parent", "home", "entries", "writable", "truncated", "scanned_entries"}
            if task.kind == "mkdir":
                allowed.add("created")
            if (set(result) - allowed or (task.kind == "mkdir" and type(result.get("created")) is not bool)
                    or not isinstance(result.get("entries"), list) or len(result["entries"]) > 200
                    or not isinstance(result.get("path"), str)):
                raise BrokerError("INVALID_RESULT", "Invalid directory listing.")
            task.state, task.result = "succeeded", result
        elif task.kind == "configure":
            idle(connection)
            config = document.get("configuration")
            profile = next((p for p in worker.profiles if p.get("alias") == connection.configuration["credential_profile"]), None)
            def active_site(value):
                return {key: item for key, item in value.items() if key not in TIMING_FIELDS} if isinstance(value, dict) else None
            if (result.get("ok") is not True or result.get("writable") is not True
                    or not isinstance(config, dict) or set(config) != PUBLIC_CONFIG_FIELDS or not profile
                    or any(config.get(key) != connection.configuration.get(key) for key in ("backend", "ssh", "credential_profile"))
                    or active_site(config.get("site_profile")) != active_site(task.request["site_profile"])
                    or result.get("module_availability_enabled", False) is not task.request.get("module_availability_enabled", False)
                    or not canonical_root(config.get("remote_root")) or config.get("remote_root") != result.get("path")
                    or config.get("remote_root") not in profile["remote_root_prefixes"]
                    or active_site(config.get("site_profile")) != active_site(profile["site_profile"])
                    or config.get("credential_profile_revision_sha256") != profile["revision_sha256"]):
                raise BrokerError("INVALID_CONFIGURATION", "The connector did not confirm the requested cluster configuration.", 409)
            connection.configuration, connection.revision_sha256 = config, digest(config)
            connection.configuration_confirmed = True
            connection.setup_complete = connection.discovery_state == "ready"
            connection.save(update_fields=["configuration", "revision_sha256", "configuration_confirmed", "setup_complete"])
            registration.requested_root = config["remote_root"]
            registration.connector_settings = {**registration.connector_settings,
                "module_availability_enabled": task.request.get("module_availability_enabled", False)}
            registration.revision += 1
            registration.save(update_fields=["requested_root", "revision", "connector_settings"])
            task.state, task.result = "succeeded", {"path": config["remote_root"]}
            audit(None, task.owner_id, "cluster.configuration_updated", connection.id)
        else:
            raise BrokerError("INVALID_TASK", "Unknown connector operation.")
        task.save(update_fields=["state", "result", "error_code"])
        return {"accepted": True}
