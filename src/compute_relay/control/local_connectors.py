"""CSRF-protected human requests to the foreground local connector supervisor."""

import ipaddress
import json
from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, render
from django.views.decorators.debug import sensitive_variables

from compute_relay.connector_profiles import ConnectorProfileError, _state_directory
from compute_relay.local_connectors import LocalConnectorError, LocalConnectorRequests, connector_directory, loopback_origin
from .errors import BrokerError
from .management import human_view
from .models import ClusterRegistration, Connection, User
from .personal_connectors import require_owner, setup_document, target
from .services import audit


def local_origin(request):
    """Never turn an arbitrary Host header or bearer request into local SSH work."""
    if getattr(settings, "BROKER_LOCAL_RUNTIME", False) not in {True, "1"}:
        raise BrokerError("LOCAL_START_DISABLED", "Start connectors from the installed local application.", 403)
    if request.headers.get("Authorization"):
        raise BrokerError("HUMAN_REQUIRED", "Project and worker keys cannot start local connectors.", 403)
    try:
        origin = loopback_origin(getattr(settings, "BROKER_LOCAL_ORIGIN", ""))
        parsed = urlsplit(origin)
        remote = ipaddress.ip_address(request.META.get("REMOTE_ADDR", ""))
        if not remote.is_loopback or request.get_host() != parsed.netloc or request.scheme != parsed.scheme:
            raise ValueError
    except (ValueError, LocalConnectorError):
        raise BrokerError("LOCAL_ORIGIN_REQUIRED", "Use the loopback address opened by the local application.", 403) from None
    if set(request.POST) - {"setup_revision", "csrfmiddlewaretoken"}:
        raise BrokerError("INVALID_REQUEST", "Local startup accepts only the saved cluster revision.", 400)
    return origin


def _has_local_state(registration):
    directory = connector_directory(settings.DATA_DIR, registration.id)
    try:
        (directory / "state.json").lstat()
    except FileNotFoundError:
        return False
    try:
        _state_directory(directory)
    except (ConnectorProfileError, OSError):
        raise BrokerError("LOCAL_STATE_UNSAFE", "The saved local connector state is not private or accessible. Check it in the application terminal.", 409) from None
    return True


@sensitive_variables()
def _enqueue(request, registration, origin, *, revision_required=True):
    require_owner(request.user)
    with transaction.atomic():
        owner = User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
        require_owner(owner)
        registration = get_object_or_404(ClusterRegistration.objects.select_for_update(),
            pk=registration.pk, owner=owner, archived=False)
        if revision_required and request.POST.get("setup_revision") != str(registration.revision):
            raise BrokerError("SETUP_CHANGED", "Reload the cluster before starting its connector.", 409)
        ssh = target(registration)
        resume = _has_local_state(registration)

        def document():
            setup = None
            if not resume:
                # Issuance, revision/idle checks and audit remain in the same
                # pairing service used by an explicit setup-file download.
                response = setup_document(request, registration, revision_required=revision_required)
                setup = json.loads(response.content)
                setup["broker_url"] = origin
            return {"schema_version": "1.0", "request_id": str(uuid4()), "registration_id": str(registration.id),
                "broker_url": origin, "name": registration.name, "target": ssh, "setup": setup}

        try:
            LocalConnectorRequests(settings.DATA_DIR).enqueue(registration.id, document)
        except LocalConnectorError as error:
            raise BrokerError("LOCAL_START_UNAVAILABLE", str(error), 409) from None
        audit(None, owner.id, "connector.local_start_queued", registration.id)
    return render(request, "broker/local_connector_queued.html", {"cluster_name": registration.name,
        "cluster_url": f"/clusters/{registration.connection_id}/" if registration.connection_id else f"/clusters/setup/{registration.id}/"}, status=202)


@human_view
def start(request, registration_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    origin = local_origin(request)
    registration = get_object_or_404(ClusterRegistration, pk=registration_id, owner=request.user, archived=False)
    return _enqueue(request, registration, origin)


@human_view
def start_connection(request, connection_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    origin = local_origin(request)
    require_owner(request.user)
    with transaction.atomic():
        owner = User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
        require_owner(owner)
        connection = get_object_or_404(Connection.objects.select_for_update(), pk=connection_id,
            owner=owner, backend="slurm", archived=False)
        ssh = connection.configuration.get("ssh", {})
        registration, created = ClusterRegistration.objects.get_or_create(connection=connection, defaults={
            "owner": owner, "name": connection.name, "backend": "slurm", "host": ssh.get("host", ""),
            "remote_user": ssh.get("user", ""), "port": ssh.get("port", 22),
            "requested_root": connection.configuration.get("remote_root", "")})
        return _enqueue(request, registration, origin, revision_required=not created)
