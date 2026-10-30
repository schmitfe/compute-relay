"""Owner-confirmed archival of configuration; no scheduler or filesystem calls."""

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .errors import BrokerError
from .local_auth import current_user_active
from .management import human_view
from .models import Attempt, ClusterRegistration, Connection, ConnectorEnrollment, Project, User, WorkerIdentity
from .services import audit


def _require_owner(user):
    if not current_user_active(user):
        raise BrokerError("AUTH_REQUIRED", "A current human account is required.", 403)


def _active_configuration(registration=None, connection=None):
    if (registration is not None and registration.archived) or (connection is not None and connection.archived):
        raise BrokerError("CLUSTER_REMOVED", "This cluster configuration has already been removed.", 409)


def _unfinished(connection):
    if connection is None:
        return {"attempts": 0, "accounting": 0, "outputs": 0, "reserved_cpu": 0, "reserved_gpu": 0}
    from .cluster_services import unfinished_configuration_work
    pending = ~Q(accounting_state__in=["settled", "released"])
    outputs = Q(collection_pending=True) | Q(native_identity__isnull=False,
        accounting_state__in=["settled", "released"], collection_state__in=["pending", "retry_pending", "failed"])
    counts = Attempt.objects.filter(connection=connection).filter(unfinished_configuration_work()).aggregate(
        attempts=Count("id"), accounting=Count("id", filter=pending),
        outputs=Count("id", filter=outputs),
        reserved_cpu=Sum("reservation_cpu", filter=pending), reserved_gpu=Sum("reservation_gpu", filter=pending))
    return {key: value or 0 for key, value in counts.items()}


def _force_requested(request):
    action = request.POST.get("action", "remove")
    if action not in {"remove", "remove_anyway"}:
        raise BrokerError("INVALID_REQUEST", "Choose a removal action from the confirmation page.")
    if action == "remove_anyway" and request.POST.get("acknowledge_unfinished") != "yes":
        raise BrokerError("UNFINISHED_WORK_ACKNOWLEDGEMENT_REQUIRED",
            "Acknowledge that remote jobs may continue and unresolved accounting and budget holds will be retained before removing anyway.")
    return action == "remove_anyway"


def _confirmation(request, registration=None, connection=None):
    from .connector_setup import personal
    _active_configuration(registration, connection)
    unfinished = _unfinished(connection)
    return render(request, "broker/cluster_remove.html", {"cluster_name": registration.name if registration else connection.name,
        "cluster": connection, "setup": registration, "personal_configuration": bool(connection and personal(connection)),
        "cancel_url": f"/clusters/{connection.id}/" if connection else f"/clusters/setup/{registration.id}/",
        "setup_revision": str(registration.revision) if registration else None,
        "connection_revision_sha256": connection.revision_sha256 if connection else None,
        "busy": bool(unfinished["attempts"]), "unfinished": unfinished,
        "reserved_cpu_hours": unfinished["reserved_cpu"] / 3600, "reserved_gpu_hours": unfinished["reserved_gpu"] / 3600})


def _check_revisions(request, registration=None, connection=None):
    # A bound setup carries both revisions; a new registration appearing after
    # a connection-only confirmation requires a fresh confirmation too.
    if ((registration is not None and request.POST.get("setup_revision") != str(registration.revision))
            or (connection is not None and request.POST.get("connection_revision_sha256") != connection.revision_sha256)):
        raise BrokerError("SETUP_CHANGED", "The cluster configuration changed. Open its removal confirmation again.", 409)


def _lock_connection(connection_id, owner):
    reference = get_object_or_404(Connection, pk=connection_id, owner=owner)
    worker = WorkerIdentity.objects.select_for_update().get(pk=reference.worker_id)
    list(Project.objects.select_for_update(of=("self",)).filter(connections=connection_id).order_by("id"))
    current = get_object_or_404(Connection.objects.select_for_update(), pk=connection_id, owner=owner)
    if current.worker_id != worker.id:
        raise BrokerError("SETUP_CHANGED", "The cluster connector changed. Open its removal confirmation again.", 409)
    return current, worker


def _archive(owner, registration=None, connection=None, worker=None, *, force=False):
    _active_configuration(registration, connection)
    unfinished = _unfinished(connection)
    if unfinished["attempts"] and not force:
        raise BrokerError("CONNECTION_BUSY", "Finish or reconcile executions, output collection and accounting before removing this cluster.", 409)
    if connection is not None:
        connection.archived, connection.enabled, connection.setup_complete = True, False, False
        connection.discovery_state, connection.discovery_error, connection.auth_state = "not_started", "", "removed"
        connection.discovery_lease_id = connection.discovery_lease_expires_at = None
        connection.discovery_generation += 1
        connection.save(update_fields=["archived", "enabled", "setup_complete", "discovery_state", "discovery_error",
            "auth_state", "discovery_lease_id", "discovery_lease_expires_at", "discovery_generation"])
    if registration is not None:
        registration.archived, registration.connector_settings = True, {}
        registration.revision += 1
        registration.save(update_fields=["archived", "connector_settings", "revision"])
        ConnectorEnrollment.objects.filter(registration=registration, consumed_at__isnull=True).update(expires_at=timezone.now())
    if (worker is not None and ConnectorEnrollment.objects.filter(worker=worker, consumed_at__isnull=False).exists()
            and not Connection.objects.filter(worker=worker, archived=False).exists()):
        # Revoke an enrolled personal connector only after its last active
        # connection is archived; a fixture worker can serve several bindings.
        WorkerIdentity.objects.filter(pk=worker.pk).update(revoked=True)
    audit(None, owner.id, "cluster.removed", connection.id if connection else registration.id,
        {"registration_id": str(registration.id) if registration else None,
            "connection_id": str(connection.id) if connection else None,
            "forced": force, "unfinished": unfinished})


@human_view
def registration(request, registration_id):
    _require_owner(request.user)
    reference = get_object_or_404(ClusterRegistration, pk=registration_id, owner=request.user)
    if request.method == "GET":
        linked = get_object_or_404(Connection, pk=reference.connection_id, owner=request.user) if reference.connection_id else None
        return _confirmation(request, reference, linked)
    with transaction.atomic():
        owner = User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
        _require_owner(owner)
        current = get_object_or_404(ClusterRegistration.objects.select_for_update(), pk=reference.pk, owner=owner)
        linked, worker = _lock_connection(current.connection_id, owner) if current.connection_id else (None, None)
        _active_configuration(current, linked)
        _check_revisions(request, current, linked)
        _archive(owner, current, linked, worker, force=_force_requested(request))
    return redirect("/clusters/")


@human_view
def connection(request, connection_id):
    _require_owner(request.user)
    reference = get_object_or_404(Connection, pk=connection_id, owner=request.user)
    associated = ClusterRegistration.objects.filter(connection=reference).first()
    if associated is not None and associated.owner_id != request.user.pk:
        raise BrokerError("NOT_FOUND", "Cluster configuration not found.", 404)
    if request.method == "GET":
        return _confirmation(request, associated, reference)
    with transaction.atomic():
        owner = User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
        _require_owner(owner)
        current_registration = ClusterRegistration.objects.select_for_update().filter(connection_id=reference.id).first()
        if current_registration is not None and current_registration.owner_id != owner.pk:
            raise BrokerError("NOT_FOUND", "Cluster configuration not found.", 404)
        current, worker = _lock_connection(reference.id, owner)
        _active_configuration(current_registration, current)
        _check_revisions(request, current_registration, current)
        _archive(owner, current_registration, current, worker, force=_force_requested(request))
    return redirect("/clusters/")
