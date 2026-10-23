"""Human-authorized one-use pairing of a personal, outbound SSH connector."""

from datetime import timedelta
import json

from django.db import transaction
from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .auth import bearer, new_token, token_hash
from .errors import BrokerError
from .identity.authority import cached_user_active
from .management import human_view
from .models import Attempt, ClusterRegistration, Connection, ConnectorEnrollment, Project, User, WorkerIdentity
from .services import audit, digest
from .views import body, boundary


def require_owner(user):
    if not cached_user_active(user):
        raise BrokerError("AUTH_REQUIRED", "A current human account is required.", 403)


def target(registration):
    if registration.archived or (registration.connection_id and registration.connection.archived):
        raise BrokerError("CLUSTER_REMOVED", "This cluster configuration was removed.", 409)
    if registration.backend != "slurm" or not registration.remote_user:
        raise BrokerError("SETUP_INCOMPLETE", "Set a Slurm hostname and remote username first.", 409)
    from .cluster_setup import canonical_root
    if registration.requested_root and not canonical_root(registration.requested_root):
        raise BrokerError("SETUP_INCOMPLETE", "Set a canonical job directory first.", 409)
    return {"host": registration.host, "user": registration.remote_user, "port": registration.port}


def check_idle(registration):
    if registration.connection_id and Attempt.objects.filter(connection_id=registration.connection_id).exclude(
            accounting_state__in=["settled", "released"]).exists():
        raise BrokerError("CONNECTION_BUSY", "Finish or reconcile existing executions before pairing another connector.", 409)


def site_profile(registration):
    from compute_broker.domain.cluster_configuration import TIMING_FIELDS
    profile = {key: value for key, value in registration.connection.configuration.get("site_profile", {}).items()
        if key not in TIMING_FIELDS} if registration.connection_id else {}
    profile["automatic_execution_allowed"] = False
    return profile


@sensitive_variables()
def setup_document(request, registration, *, revision_required=True):
    require_owner(request.user)
    with transaction.atomic():
        # Same owner-first fence as execution and directory suspension.
        owner = User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
        require_owner(owner)
        registration = get_object_or_404(ClusterRegistration.objects.select_for_update(), pk=registration.pk, owner=owner, archived=False)
        if revision_required and request.POST.get("setup_revision") != str(registration.revision):
            raise BrokerError("SETUP_CHANGED", "Reload the cluster before downloading its connector setup.", 409)
        ssh = target(registration)
        check_idle(registration)
        now = timezone.now()
        ConnectorEnrollment.objects.filter(registration=registration, consumed_at__isnull=True).update(expires_at=now)
        secret = new_token("cbpair")
        enrollment = ConnectorEnrollment.objects.create(registration=registration, token_hash=token_hash(secret),
            setup_revision=registration.revision, expires_at=now + timedelta(minutes=10))
        document = {"schema_version": "1.0", "broker_url": request.build_absolute_uri("/").rstrip("/"),
            "registration_id": str(registration.id), "owner_id": str(owner.id), "name": registration.name,
            "pairing_token": secret, "expires_at": enrollment.expires_at.isoformat(),
            "target": ssh, "remote_root": registration.requested_root,
            "site_profile": site_profile(registration),
            "local": registration.connector_settings}
        audit(None, owner.id, "connector.setup_issued", enrollment.id)
    response = HttpResponse(json.dumps(document, indent=2) + "\n", content_type="application/json")
    response["Content-Disposition"] = 'attachment; filename="personal-connector.json"'
    return response


@human_view
def download(request, registration_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    registration = get_object_or_404(ClusterRegistration, id=registration_id, owner=request.user, archived=False)
    return setup_document(request, registration)


@human_view
def download_connection(request, connection_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    require_owner(request.user)
    with transaction.atomic():
        owner = User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
        require_owner(owner)
        connection = get_object_or_404(Connection.objects.select_for_update(), id=connection_id, owner=owner, backend="slurm", archived=False)
        ssh = connection.configuration.get("ssh", {})
        registration, created = ClusterRegistration.objects.get_or_create(connection=connection, defaults={
            "owner": owner, "name": connection.name, "backend": "slurm", "host": ssh.get("host", ""),
            "remote_user": ssh.get("user", ""), "port": ssh.get("port", 22),
            "requested_root": connection.configuration.get("remote_root", "")})
        return setup_document(request, registration, revision_required=not created)


@boundary("POST")
@sensitive_variables()
def enroll(request):
    from .cluster_services import publish_profiles
    token = bearer(request)
    found = ConnectorEnrollment.objects.filter(token_hash=token_hash(token)).select_related("registration").first()
    if found is None:
        raise BrokerError("PAIRING_INVALID", "Pairing is invalid or expired. Download a new setup file.", 403)
    document = body(request, maximum=16 * 1024)
    profile = document.get("profile")
    with transaction.atomic():
        owner = User.objects.select_for_update(no_key=True).get(pk=found.registration.owner_id)
        require_owner(owner)
        registration = ClusterRegistration.objects.select_for_update().get(pk=found.registration_id, owner=owner)
        pairing = ConnectorEnrollment.objects.select_for_update().get(pk=found.pk)
        if pairing.consumed_at is not None or pairing.expires_at <= timezone.now() or pairing.setup_revision != registration.revision:
            raise BrokerError("PAIRING_INVALID", "Pairing was consumed, expired or changed. Download a new setup file.", 409)
        ssh = target(registration)
        previous_worker_id = None
        if registration.connection_id:
            previous_worker_id = Connection.objects.values_list("worker_id", flat=True).get(pk=registration.connection_id)
            WorkerIdentity.objects.select_for_update().get(pk=previous_worker_id)
            list(Project.objects.select_for_update(of=("self",)).filter(connections=registration.connection_id).order_by("id"))
            current = Connection.objects.select_for_update().get(pk=registration.connection_id, owner=owner)
            if current.worker_id != previous_worker_id:
                raise BrokerError("SETUP_CHANGED", "The cluster's connector changed; download a fresh setup file.", 409)
            registration.connection = current
        # The local connector chooses a private runtime, but cannot widen the
        # target or grant itself an automatic-execution site policy.
        from .cluster_setup import canonical_root
        roots = profile.get("remote_root_prefixes") if isinstance(profile, dict) else None
        if (not isinstance(profile, dict) or profile.get("backend") != "slurm"
                or profile.get("allowed_hosts") != [ssh["host"]] or profile.get("allowed_users") != [ssh["user"]]
                or profile.get("allowed_ports") != [ssh["port"]]
                or not isinstance(roots, list) or len(roots) != 1 or not canonical_root(roots[0])
                or (registration.requested_root and roots != [registration.requested_root])
                or profile.get("site_profile") != site_profile(registration)):
            raise BrokerError("PAIRING_SCOPE", "Connector scope does not match the saved cluster.", 403)
        check_idle(registration)
        secret = new_token("cbw")
        worker = WorkerIdentity.objects.create(owner=owner, name=registration.name[:69] + " connector", token_hash=token_hash(secret))
        publish_profiles(worker, {"profiles": [profile], "extensions": []})
        configuration = {"backend": "slurm", "credential_profile": profile["alias"], "ssh": ssh,
            "remote_root": roots[0], "site_profile": site_profile(registration),
            "credential_profile_revision_sha256": profile["revision_sha256"]}
        if registration.connection_id:
            connection = Connection.objects.select_for_update().get(pk=registration.connection_id, owner=owner)
            old_worker = connection.worker
            # Re-check after the dispatch lock: no already leased job may be
            # moved to a new identity, even if it appeared idle before locking.
            check_idle(registration)
            if connection.auth_state == "worker_removed":
                # Replacing a deliberately removed worker is an explicit human
                # pairing action; discovery and configuration confirmation are
                # still required before this connection can execute new jobs.
                connection.enabled = True
            connection.worker = worker
            connection.configuration, connection.revision_sha256 = configuration, digest(configuration)
            connection.setup_complete, connection.discovery_state, connection.auth_state = False, "queued", "discovery_required"
            connection.configuration_confirmed = False
            connection.discovery_lease_id = connection.discovery_lease_expires_at = None
            connection.save()
            if (ConnectorEnrollment.objects.filter(worker=old_worker, consumed_at__isnull=False).exists()
                    and not Connection.objects.filter(worker=old_worker).exists()):
                WorkerIdentity.objects.filter(pk=old_worker.pk).update(revoked=True)
        else:
            connection = Connection.objects.create(owner=owner, worker=worker, name=registration.name, backend="slurm",
                configuration=configuration, revision_sha256=digest(configuration), setup_complete=False,
                configuration_confirmed=False,
                auth_state="discovery_required", discovery_state="queued")
            registration.connection = connection
        registration.revision += 1
        registration.requested_root = roots[0]
        registration.save(update_fields=["connection", "revision", "requested_root"])
        pairing.worker, pairing.consumed_at = worker, timezone.now()
        pairing.save(update_fields=["worker", "consumed_at"])
        audit(None, owner.id, "connector.paired", worker.id, {"connection_id": str(connection.id)})
    response = HttpResponse(json.dumps({"worker_id": str(worker.id), "worker_token": secret,
        "owner_id": str(owner.id), "connection_id": str(connection.id)}), content_type="application/json", status=201)
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "no-referrer"
    return response
