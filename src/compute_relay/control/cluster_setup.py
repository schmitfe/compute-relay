"""Saved cluster details for the personal connector setup flow."""

import posixpath
import re

from django import forms
from django.db import transaction
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from .errors import BrokerError
from .management import human_view
from .models import ClusterRegistration
from .services import audit


def owned_registrations(user):
    # Personal setup can enroll credentials for its owner, including when the
    # caller has administrator privileges elsewhere in the portal.
    return ClusterRegistration.objects.filter(owner=user, archived=False)


def canonical_root(value):
    return bool(isinstance(value, str) and re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
        and value != "/" and "//" not in value and posixpath.normpath(value) == value
        and not any(part in {".", ".."} for part in value.split("/")))


class RegistrationForm(forms.Form):
    name = forms.CharField(max_length=80, label="Cluster name")
    host = forms.CharField(max_length=253, label="Login hostname")
    user = forms.CharField(max_length=80, required=False, label="Remote username",
        help_text="Can be filled in later, before connecting the cluster.")
    port = forms.IntegerField(min_value=1, max_value=65535, initial=22)
    remote_root = forms.CharField(max_length=240, required=False, label="Working directory hint (optional)",
        help_text="You can choose this after connecting. The running connector lets you browse remote directories and confirm the baseline working directory.")
    identity_file = forms.CharField(max_length=4096, required=False, strip=False,
        label="Local SSH key path (optional)",
        help_text="Leave empty to choose your key in a native file picker on your device when the connector starts. Or enter a local path such as ~/.ssh/id_ed25519. Only the path is saved; the key and passphrase stay local.")
    known_hosts = forms.CharField(max_length=4096, required=False, strip=False,
        label="Local known-hosts path (optional)",
        help_text="Use an absolute path or ~/.ssh/known_hosts on your device. If omitted, the connector manages its own host-key confirmation locally.")
    session_minutes = forms.IntegerField(required=False, min_value=1, max_value=1440,
        initial=480, label="SSH session lifetime (minutes)",
        help_text="Default: 480 minutes (8 hours). The cluster may require you to sign in again sooner.")
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

    def clean(self):
        data = super().clean()
        if self.data.get("backend", "slurm") != "slurm":
            self.add_error(None, "Only Slurm clusters can be added here.")
        data["backend"] = "slurm"
        settings = {"session_seconds": (data.get("session_minutes") or 480) * 60}
        for field in ("identity_file", "known_hosts"):
            value = data.get(field, "")
            if not value:
                continue
            if any(char in value for char in "\r\n\x00") or not value.startswith(("/", "~/")):
                self.add_error(field, "Use an absolute path or a path beginning with ~/ without line breaks or NUL characters.")
            else:
                settings[field] = value
        data["connector_settings"] = settings
        return data

    @property
    def advanced_fields(self):
        return [self[name] for name in ("port", "remote_root", "identity_file", "known_hosts", "session_minutes")]

    @property
    def advanced_open(self):
        return any(field.errors for field in self.advanced_fields)


@human_view
def detail(request, registration_id):
    registration = get_object_or_404(owned_registrations(request.user), id=registration_id)
    if registration.connection_id:
        return redirect(f"/clusters/{registration.connection_id}/")
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    return render(request, "broker/cluster_setup.html", {"setup": registration})


@human_view
def edit(request, registration_id):
    registration = get_object_or_404(owned_registrations(request.user), id=registration_id)
    if registration.connection_id:
        raise BrokerError("SETUP_ALREADY_BOUND", "An existing connection cannot be changed through setup. Register a new cluster binding.", 409)
    form = RegistrationForm(request.POST if request.method == "POST" else None, initial={
        "name": registration.name, "backend": registration.backend, "host": registration.host,
        "user": registration.remote_user, "port": registration.port, "remote_root": registration.requested_root,
        **{key: value for key, value in registration.connector_settings.items()
           if key in {"identity_file", "known_hosts"}},
        "session_minutes": registration.connector_settings.get("session_seconds", 28800) // 60,
        "setup_revision": registration.revision})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            current = get_object_or_404(owned_registrations(request.user).select_for_update(), id=registration_id)
            if current.connection_id or form.cleaned_data.get("setup_revision") != current.revision:
                raise BrokerError("SETUP_CHANGED", "This setup changed. Reload before saving.", 409)
            data = form.cleaned_data
            for field, value in {"name": data["name"], "backend": data["backend"], "host": data["host"],
                    "remote_user": data["user"], "port": data["port"], "requested_root": data["remote_root"],
                    "connector_settings": data["connector_settings"]}.items():
                setattr(current, field, value)
            current.revision += 1
            current.save()
            audit(None, request.user.id, "cluster.setup_updated", current.id)
        return redirect(f"/clusters/setup/{registration.id}/")
    return render(request, "broker/cluster_form.html", {"form": form, "setup": registration})
