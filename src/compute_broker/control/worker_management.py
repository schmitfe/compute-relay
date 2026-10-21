"""Human worker registration; plaintext tokens exist only in the first response."""

from uuid import UUID, uuid4

from django import forms
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, render

from . import services
from .auth import new_token, token_hash
from .errors import BrokerError
from .management import human_view
from .models import ClusterRegistration, User, WorkerIdentity


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
