"""Human worker registration; plaintext tokens exist only in the first response."""

from uuid import UUID, uuid4

from django import forms
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import services
from .auth import new_token, token_hash
from .errors import BrokerError
from .identity.authority import cached_user_active
from .identity.models import AuthenticationIdentity
from .management import human_view
from .models import (Attempt, ClusterRegistration, Connection, ConnectorEnrollment, ConnectorTask,
    Project, User, WorkerIdentity)


class WorkerRegistrationForm(forms.Form):
    name = forms.CharField(max_length=80, label="Worker name",
        help_text="A name for the protected device or server that will connect to your clusters.")
    submission_id = forms.UUIDField(widget=forms.HiddenInput)
    setup = forms.UUIDField(required=False, widget=forms.HiddenInput)


def owned_setup(user, value):
    if value in (None, ""):
        return None
    try:
        identity = UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise Http404 from None
    return get_object_or_404(ClusterRegistration, id=identity, owner=user)


def issue_worker(user, name, submission_id):
    """Owner lock and globally unique nonce prevent concurrent secret reissue."""
    if not user.is_authenticated:
        raise BrokerError("HUMAN_REQUIRED", "A human session is required.", 403)
    form = WorkerRegistrationForm({"name": name, "submission_id": submission_id})
    if not form.is_valid():
        raise BrokerError("INVALID_SUBMISSION", "Open the worker form again and enter a name.")
    nonce = form.cleaned_data["submission_id"]
    try:
        with transaction.atomic():
            owner = User.objects.select_for_update().get(id=user.id)
            if not owner.is_active:
                raise BrokerError("HUMAN_REQUIRED", "An active human account is required.", 403)
            if WorkerIdentity.objects.filter(enrollment_nonce=nonce).exists():
                raise BrokerError("SECRET_ALREADY_SHOWN", "This registration was already processed. Its token cannot be shown again; register a new worker if it was lost.", 409)
            secret = new_token("cbw")
            worker = WorkerIdentity.objects.create(owner=owner, name=form.cleaned_data["name"],
                token_hash=token_hash(secret), enrollment_nonce=nonce)
            services.audit(None, owner.id, "worker.registered", worker.id)
    except IntegrityError:
        # Different owners have different row locks; the global nonce constraint
        # also fences simultaneous cross-owner replays. Return no worker metadata.
        if WorkerIdentity.objects.filter(enrollment_nonce=nonce).exists():
            raise BrokerError("SECRET_ALREADY_SHOWN", "This registration was already processed. Its token cannot be shown again; register a new worker if it was lost.", 409) from None
        raise
    return worker, secret


@human_view
def worker_new(request):
    setup = owned_setup(request.user, request.GET.get("setup"))
    form = WorkerRegistrationForm(request.POST if request.method == "POST" else None,
        initial={"submission_id": uuid4(), "setup": setup.id if setup else None})
    if request.method == "POST" and form.is_valid():
        selected = owned_setup(request.user, form.cleaned_data["setup"])
        if setup is not None and (selected is None or selected.id != setup.id):
            raise BrokerError("INVALID_SETUP", "Open the worker form from the intended cluster setup again.")
        worker, secret = issue_worker(request.user, form.cleaned_data["name"], form.cleaned_data["submission_id"])
        return render(request, "broker/worker_created.html", {"worker": worker, "secret": secret, "setup": selected}, status=201)
    return render(request, "broker/worker_form.html", {"form": form, "setup": setup})


@human_view
def worker_detail(request, worker_id):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    worker = get_object_or_404(WorkerIdentity, id=worker_id, owner=request.user)
    setup = owned_setup(request.user, request.GET.get("setup"))
    return render(request, "broker/worker_detail.html", {"worker": worker, "setup": setup})


def _require_removal_owner(user):
    if not cached_user_active(user):
        raise BrokerError("AUTH_REQUIRED", "Current account verification is required before removing a worker.", 403)


def _removal_revision(worker, connections, registrations):
    return services.digest({"worker_id": str(worker.id),
        "connections": [{"id": str(item.id), "revision": item.revision_sha256,
            "enabled": item.enabled, "archived": item.archived} for item in connections],
        "registrations": [{"id": str(item.id), "revision": item.revision} for item in registrations]})


def _worker_busy(worker):
    # Terminal scheduler state alone does not establish final accounting. In
    # particular, submission uncertainty and settlement must remain observable.
    return Attempt.objects.filter(connection__worker=worker).exclude(
        accounting_state__in=["settled", "released"]).exists()


def remove_worker(user, worker_id, confirmation):
    """Revoke an idle owned worker while preserving its bindings and history."""
    _require_removal_owner(user)
    with transaction.atomic():
        owner = User.objects.select_for_update(no_key=True).get(id=user.id)
        list(AuthenticationIdentity.objects.select_for_update().filter(user=owner))
        _require_removal_owner(owner)
        # Match cluster removal and personal re-pair: owner/directory authority,
        # registration, worker, projects, connections. Never stop a worker first
        # and then discover that it was needed to finish unsettled accounting.
        registrations = list(ClusterRegistration.objects.select_for_update().filter(
            owner=owner, connection__worker_id=worker_id).order_by("id"))
        worker = get_object_or_404(WorkerIdentity.objects.select_for_update(), id=worker_id, owner=owner)
        if worker.revoked:
            return False
        # Worker authority now prevents a concurrent bind/re-pair/dispatch from
        # moving these connections or creating a new dispatch after the check.
        connection_ids = list(Connection.objects.filter(worker=worker).values_list("id", flat=True))
        list(Project.objects.select_for_update(of=("self",)).filter(
            connections__in=connection_ids).order_by("id"))
        connections = list(Connection.objects.select_for_update().filter(worker=worker).order_by("id"))
        if confirmation != _removal_revision(worker, connections, registrations):
            raise BrokerError("WORKER_CHANGED", "The worker's cluster bindings changed. Open its removal confirmation again.", 409)
        if _worker_busy(worker):
            raise BrokerError("WORKER_BUSY", "Finish or reconcile this worker's executions and their accounting before removing it.", 409)
        now = timezone.now()
        worker.revoked = True
        worker.save(update_fields=["revoked"])
        # Scope owned setup intents to this account. A different account's
        # replacement pairing grants a different worker and is not our token.
        registration_ids = [item.id for item in registrations]
        ConnectorEnrollment.objects.filter(Q(registration_id__in=registration_ids) | Q(worker=worker),
            consumed_at__isnull=True).update(expires_at=now)
        ClusterRegistration.objects.filter(id__in=registration_ids).update(revision=F("revision") + 1)
        Connection.objects.filter(id__in=connection_ids).update(enabled=False, setup_complete=False,
            configuration_confirmed=False, auth_state="worker_removed", discovery_state="not_started",
            discovery_error="", discovery_lease_id=None, discovery_lease_expires_at=None,
            discovery_generation=F("discovery_generation") + 1)
        ConnectorTask.objects.filter(connection_id__in=connection_ids, state__in=["queued", "running"]).update(
            state="failed", error_code="WORKER_REMOVED", lease_id=None, lease_expires_at=None,
            generation=F("generation") + 1)
        services.audit(None, owner.id, "worker.removed", worker.id,
            {"connection_ids": [str(item.id) for item in connections if item.owner_id == owner.id]})
    return True


@human_view
def worker_remove(request, worker_id):
    _require_removal_owner(request.user)
    worker = get_object_or_404(WorkerIdentity, id=worker_id, owner=request.user)
    if request.method == "POST":
        remove_worker(request.user, worker_id, request.POST.get("confirmation"))
        return redirect("/clusters/")
    connections = list(Connection.objects.filter(worker=worker).order_by("id"))
    registrations = list(ClusterRegistration.objects.filter(owner=request.user, connection__worker=worker).order_by("id"))
    return render(request, "broker/worker_remove.html", {"worker": worker,
        "clusters": [item for item in connections if item.owner_id == request.user.id],
        "other_cluster_count": sum(item.owner_id != request.user.id for item in connections),
        "busy": _worker_busy(worker), "confirmation": _removal_revision(worker, connections, registrations)})
