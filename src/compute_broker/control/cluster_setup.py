"""Human cluster setup intent, followed by an explicitly approved worker binding."""

import json
import posixpath
import re
from uuid import UUID

from django import forms
from django.db import transaction
from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from .errors import BrokerError
from .forms import owned_workers
from .management import human_view
from .models import ClusterRegistration, Connection, WorkerIdentity
from .services import audit, digest


def owned_registrations(user):
    rows = ClusterRegistration.objects.all()
    return rows if user.is_superuser else rows.filter(owner=user)


def canonical_root(value):
    return bool(isinstance(value, str) and re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
        and value != "/" and "//" not in value and posixpath.normpath(value) == value
        and not any(part in {".", ".."} for part in value.split("/")))


class RegistrationForm(forms.Form):
    name = forms.CharField(max_length=80, label="Cluster name")
    backend = forms.ChoiceField(choices=[("slurm", "Slurm"), ("fake", "Simulator (testing)")], initial="slurm")
    host = forms.CharField(max_length=253, label="Login hostname")
    user = forms.CharField(max_length=80, required=False, label="Remote username",
        help_text="Can be filled in later, before connecting the cluster.")
    port = forms.IntegerField(min_value=1, max_value=65535, initial=22)
    remote_root = forms.CharField(max_length=240, required=False, label="Proposed remote job directory",
        help_text="Optional. You may propose a new directory; this only saves the plan. A protected worker must approve its mapping before use.")
    setup_revision = forms.IntegerField(required=False, min_value=1, widget=forms.HiddenInput)

    def clean_host(self):
        value = self.cleaned_data["host"]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", value) or value.startswith("-"):
            raise forms.ValidationError("Use a hostname or IP address without SSH options or a username.")
        return value

    def clean_user(self):
        value = self.cleaned_data["user"]
        if value and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}", value):
            raise forms.ValidationError("Use a remote account name without shell syntax.")
        return value

    def clean_remote_root(self):
        value = self.cleaned_data["remote_root"]
        if value and not canonical_root(value):
            raise forms.ValidationError("Use a canonical absolute directory without shell syntax, traversal or a trailing slash.")
        return value


class BindingForm(forms.Form):
    worker = forms.ModelChoiceField(queryset=WorkerIdentity.objects.none(), widget=forms.HiddenInput)
    credential_profile = forms.ChoiceField(label="Advertised credential profile")
    remote_root = forms.CharField(max_length=240, label="Approved remote job directory")
    setup_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def __init__(self, *args, user, registration, worker=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.registration = registration
        self.fields["worker"].queryset = owned_workers(user)
        self.fields["setup_revision"].initial = registration.revision
        self.fields["remote_root"].initial = registration.requested_root
        value = self.data.get("worker") if self.is_bound else getattr(worker, "id", None)
        try:
            self.worker = owned_workers(user).filter(id=UUID(str(value))).first()
        except (ValueError, TypeError, AttributeError):
            self.worker = None
        self.fields["worker"].initial = self.worker
        self.fields["credential_profile"].choices = [(profile_choice(item), item["alias"])
            for item in (self.worker.profiles if self.worker else []) if item.get("backend") == registration.backend]

    def clean(self):
        data = super().clean()
        worker = data.get("worker")
        if worker and "credential_profile" in data and "remote_root" in data:
            try:
                data["profile"] = validate_binding(self.registration, worker, data["credential_profile"], data["remote_root"])
            except BrokerError as exc:
                self.add_error(None, exc.message)
        return data


def profile_choice(profile):
    return profile["alias"] + ":" + profile["revision_sha256"]


def validate_binding(registration, worker, selected, root):
    if worker.revoked or worker.owner_id != registration.owner_id:
        raise BrokerError("WORKER_DENIED", "Choose a current worker assigned to this cluster's owner.", 403)
    profile = next((item for item in worker.profiles if profile_choice(item) == selected), None)
    if profile is None:
        raise BrokerError("PROFILE_CHANGED", "The worker profile changed or has not been advertised. Reload its profiles.", 409)
    if (profile["backend"] != registration.backend or registration.host not in profile["allowed_hosts"]
            or not registration.remote_user or registration.remote_user not in profile["allowed_users"]
            or registration.port not in profile["allowed_ports"]):
        raise BrokerError("TARGET_NOT_ALLOWED", "The saved login target is outside this profile. Update the protected worker configuration or edit the setup details.", 409)
    if not canonical_root(root) or not any(root == prefix or root.startswith(prefix + "/") for prefix in profile["remote_root_prefixes"]):
        raise BrokerError("ROOT_NOT_APPROVED", "This directory is not yet approved by the worker. Add its mapping to the protected profile, then reload profiles.", 409)
    return profile


def bind_registration(user, registration_id, data):
    with transaction.atomic():
        registration = get_object_or_404(owned_registrations(user).select_for_update(), id=registration_id)
        if registration.connection_id:
            raise BrokerError("SETUP_ALREADY_BOUND", "This cluster is already connected. Use a new setup for another execution binding.", 409)
        if data["setup_revision"] != registration.revision:
            raise BrokerError("SETUP_CHANGED", "The saved cluster details changed. Reload this setup before connecting it.", 409)
        worker = get_object_or_404(owned_workers(user).select_for_update(), id=data["worker"].id)
        profile = validate_binding(registration, worker, data["credential_profile"], data["remote_root"])
        configuration = {"backend": registration.backend, "credential_profile": profile["alias"],
            "ssh": {"host": registration.host, "user": registration.remote_user, "port": registration.port},
            "remote_root": data["remote_root"], "site_profile": profile["site_profile"],
            "credential_profile_revision_sha256": profile["revision_sha256"]}
        connection = Connection.objects.create(owner=registration.owner, worker=worker, name=registration.name,
            backend=registration.backend, configuration=configuration, revision_sha256=digest(configuration),
            setup_complete=False, auth_state="discovery_required", discovery_state="queued")
        registration.connection = connection
        registration.revision += 1
        registration.save(update_fields=["connection", "revision"])
        audit(None, user.id, "cluster.setup_bound", registration.id, {"connection_id": str(connection.id)})
        return connection


@human_view
def detail(request, registration_id):
    registration = get_object_or_404(owned_registrations(request.user), id=registration_id)
    if registration.connection_id:
        return redirect(f"/clusters/{registration.connection_id}/")
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    return render(request, "broker/cluster_setup.html", {"setup": registration,
        "workers": owned_workers(request.user), "worker_count": owned_workers(request.user).count()})


@human_view
def edit(request, registration_id):
    registration = get_object_or_404(owned_registrations(request.user), id=registration_id)
    if registration.connection_id:
        raise BrokerError("SETUP_ALREADY_BOUND", "An existing connection cannot be changed through setup. Register a new cluster binding.", 409)
    form = RegistrationForm(request.POST if request.method == "POST" else None, initial={
        "name": registration.name, "backend": registration.backend, "host": registration.host,
        "user": registration.remote_user, "port": registration.port, "remote_root": registration.requested_root,
        "setup_revision": registration.revision})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            current = get_object_or_404(owned_registrations(request.user).select_for_update(), id=registration_id)
            if current.connection_id or form.cleaned_data.get("setup_revision") != current.revision:
                raise BrokerError("SETUP_CHANGED", "This setup changed. Reload before saving.", 409)
            data = form.cleaned_data
            for field, value in {"name": data["name"], "backend": data["backend"], "host": data["host"],
                    "remote_user": data["user"], "port": data["port"], "requested_root": data["remote_root"]}.items():
                setattr(current, field, value)
            current.revision += 1
            current.save()
            audit(None, request.user.id, "cluster.setup_updated", current.id)
        return redirect(f"/clusters/setup/{registration.id}/")
    return render(request, "broker/cluster_form.html", {"form": form, "setup": registration})


@human_view
def bind(request, registration_id):
    registration = get_object_or_404(owned_registrations(request.user), id=registration_id)
    if registration.connection_id:
        if request.method == "POST":
            raise BrokerError("SETUP_ALREADY_BOUND", "This cluster already has a connection.", 409)
        return redirect(f"/clusters/{registration.connection_id}/")
    workers = owned_workers(request.user)
    selected = request.POST.get("worker") if request.method == "POST" else request.GET.get("worker")
    worker = None
    if selected:
        try:
            worker = get_object_or_404(workers, id=UUID(selected))
        except (ValueError, TypeError):
            raise BrokerError("INVALID_WORKER", "Choose a valid worker.") from None
    elif workers.count() == 1:
        worker = workers.first()
    form = BindingForm(request.POST if request.method == "POST" else None,
        user=request.user, registration=registration, worker=worker)
    if request.method == "POST" and form.is_valid():
        connection = bind_registration(request.user, registration.id, form.cleaned_data)
        return redirect(f"/clusters/{connection.id}/")
    return render(request, "broker/cluster_binding.html", {"setup": registration, "form": form,
        "workers": workers, "selected_worker": worker,
        "profiles": [item for item in (worker.profiles if worker else []) if item.get("backend") == registration.backend]})


@human_view
def worker_config(request, registration_id):
    registration = get_object_or_404(owned_registrations(request.user), id=registration_id)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    if not registration.remote_user or not registration.requested_root:
        raise BrokerError("SETUP_INCOMPLETE", "Fill in the remote username and proposed directory before downloading a worker template.", 409)
    profile = {"backend": registration.backend, "allowed_hosts": [registration.host],
        "allowed_users": [registration.remote_user], "allowed_ports": [registration.port],
        "remote_root_prefixes": [registration.requested_root], "profile_revision": 1,
        "site_profile": {"automatic_execution_allowed": False}}
    if registration.backend == "slurm":
        profile.update(ssh={"mode": "key", "identity_file": "/REPLACE_WITH_WORKER_PRIVATE_KEY_PATH",
            "known_hosts": "/REPLACE_WITH_PINNED_KNOWN_HOSTS_PATH"}, python_path="/usr/bin/python3")
    else:
        profile["state_directory"] = "/REPLACE_WITH_PRIVATE_SIMULATOR_STATE_DIRECTORY"
    configuration = {"owner_id": str(registration.owner_id), "connections": {}, "credential_profiles": {"cluster-" + registration.id.hex[:16]: profile}, "extensions": []}
    response = HttpResponse(json.dumps(configuration, indent=2) + "\n", content_type="application/json")
    response["Content-Disposition"] = 'attachment; filename="worker-template.json"'
    return response
