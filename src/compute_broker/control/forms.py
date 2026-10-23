"""Human management forms. No form accepts an SSH key or agent-supplied shell."""

import re
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR
from uuid import UUID, uuid4

from django import forms
from django.db.models import Q
from django.utils import timezone

from .models import Connection, Environment, WorkerIdentity

SCOPES = [(value, label) for value, label in (
    ("jobs:read", "Read jobs and logs"), ("objects:write", "Upload input files"),
    ("jobs:write", "Prepare jobs"), ("jobs:execute", "Request execution"),
    ("jobs:cancel", "Request cancellation"), ("artifacts:read", "Download results"))]
ALL_SCOPES = [key for key, _ in SCOPES]


def owned_connections(user):
    rows = Connection.objects.filter(archived=False)
    return rows if user.is_superuser else rows.filter(owner=user)


def owned_environments(user):
    return Environment.objects.all() if user.is_superuser else Environment.objects.filter(
        Q(owner=user) | (Q(owner__isnull=True) & (Q(connection__owner=user) | Q(project__owner=user)))).distinct()


def owned_workers(user):
    workers = WorkerIdentity.objects.filter(revoked=False)
    return workers if user.is_superuser else workers.filter(owner=user)


class LocalDateTime(forms.DateTimeField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}))
        super().__init__(*args, **kwargs)


class ProjectForm(forms.Form):
    name = forms.CharField(max_length=100, label="Project name")
    mode = forms.ChoiceField(choices=[("approval_required", "Review every execution"), ("budgeted", "Automatic within limits"), ("mixed", "Automatic within limits, review larger requests")])
    cpu_hours = forms.DecimalField(min_value=0, max_value=Decimal(2**53 - 1) / 3600, decimal_places=6, label="Total CPU core hours")
    gpu_hours = forms.DecimalField(min_value=0, max_value=Decimal(2**53 - 1) / 3600, decimal_places=6, label="Total GPU device hours")
    max_cpus = forms.IntegerField(min_value=1, max_value=1024, initial=2, label="CPUs per job")
    max_gpus = forms.IntegerField(min_value=0, max_value=16, initial=0, label="GPUs per job")
    memory_mib = forms.IntegerField(min_value=128, max_value=16777216, initial=1024, label="Memory per job (MiB)")
    wall_minutes = forms.IntegerField(min_value=1, max_value=10080, initial=10, label="Scheduler time per job (minutes)")
    max_active_jobs = forms.IntegerField(min_value=1, max_value=100, initial=2, label="Concurrent jobs")
    max_total_jobs = forms.IntegerField(required=False, min_value=0, max_value=1000000, initial=1000,
        label="Total submission limit", help_text="Lifetime execution authorizations across every API key. Failures and cancellations still count. Zero blocks new executions.")
    expires_at = LocalDateTime(label="New execution allowed until (Europe/Berlin)")
    connections = forms.ModelMultipleChoiceField(queryset=Connection.objects.none(), required=False, widget=forms.CheckboxSelectMultiple, label="Allowed clusters")
    environments = forms.ModelMultipleChoiceField(queryset=Environment.objects.none(), required=False, widget=forms.CheckboxSelectMultiple, label="Allowed environments")
    allowed_gpu_types = forms.CharField(required=False, label="Allowed GPU types", help_text="Comma-separated Slurm GRES types, for example a6000, h200.")
    paused = forms.BooleanField(required=False, label="Pause new executions")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["connections"].queryset = owned_connections(user).filter(setup_complete=True)
        self.pending_connections = owned_connections(user).filter(setup_complete=False).order_by("name", "id")
        self.fields["connections"].help_text = "Only clusters with completed setup can be granted to a project."
        if self.is_bound:
            values = self.data.getlist("connections") if hasattr(self.data, "getlist") else self.data.get("connections", [])
        else:
            values = self.initial.get("connections", owned_connections(user))
        if isinstance(values, (str, UUID)):
            values = [values]
        connection_ids = []
        for value in values:
            try:
                connection_ids.append(UUID(str(getattr(value, "pk", value))))
            except (ValueError, TypeError, AttributeError):
                pass  # The connection field reports invalid/foreign selections.
        permitted = owned_connections(user).filter(id__in=connection_ids)
        environments = owned_environments(user).filter(connection__in=permitted).select_related("connection").order_by("connection__name", "connection_id", "name")
        self.fields["environments"].queryset = environments
        groups = {}
        for environment in environments:
            groups.setdefault(environment.connection, []).append((str(environment.id), environment.name))
        self.fields["environments"].choices = [(connection.name, choices) for connection, choices in groups.items()]
        self.fields["environments"].label = "Allowed environments by cluster"
        self.fields["environments"].help_text = "Recipes belong to their cluster. Save cluster changes to load its available recipes. Unassigned legacy recipes must be recreated for a cluster."
        self.fields["expires_at"].initial = timezone.localtime(timezone.now() + timedelta(days=30))

    def clean(self):
        data = super().clean()
        if data.get("max_total_jobs") is None and "max_total_jobs" not in self.errors:
            data["max_total_jobs"] = 1000
        if data.get("expires_at") and data["expires_at"] <= timezone.now():
            self.add_error("expires_at", "Choose a future cutoff.")
        types = [part.strip() for part in data.get("allowed_gpu_types", "").split(",") if part.strip()]
        if any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}", part) for part in types):
            self.add_error("allowed_gpu_types", "Use valid Slurm GPU type names.")
        data["gpu_types"] = sorted(set(types))
        for name in ("cpu_hours", "gpu_hours"):
            if name in data:
                data[name.replace("hours", "seconds")] = int((data[name] * 3600).to_integral_value(rounding=ROUND_FLOOR))
        return data


class KeyForm(forms.Form):
    name = forms.CharField(max_length=80, label="Key name", help_text="Give each agent or client its own key.")
    expires_at = LocalDateTime(label="Key expires (Europe/Berlin)")
    scopes = forms.MultipleChoiceField(choices=SCOPES, widget=forms.CheckboxSelectMultiple, initial=ALL_SCOPES)
    cluster_access = forms.ChoiceField(choices=[("inherit", "All clusters allowed by the project"), ("restrict", "Only the selected clusters (none means deny all)")])
    execution_mode = forms.ChoiceField(required=False, initial="inherit", label="Execution confirmation",
        choices=[("inherit", "Follow project policy"), ("approval_required", "Always require human confirmation"),
                 ("automatic", "Automatic within project policy")],
        help_text="Automatic execution requires a project that permits it. Every mode respects project limits and cluster access.")
    allowed_connections = forms.ModelMultipleChoiceField(queryset=Connection.objects.none(), required=False, widget=forms.CheckboxSelectMultiple)
    submission_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid4)

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        from compute_broker.extensions import metadata_items
        self.fields["scopes"].choices = [*SCOPES, *metadata_items("KEY_SCOPES")]
        self.fields["allowed_connections"].queryset = project.connections.all()
        self.fields["expires_at"].initial = timezone.localtime(timezone.now() + timedelta(days=30))

    def clean_expires_at(self):
        value = self.cleaned_data["expires_at"]
        if value <= timezone.now() or value > timezone.now() + timedelta(days=366):
            raise forms.ValidationError("Choose an expiry within the next year.")
        return value

    def clean_execution_mode(self):
        return self.cleaned_data.get("execution_mode") or "inherit"


class ClusterForm(forms.Form):
    name = forms.CharField(max_length=80, label="Cluster name")
    worker = forms.ModelChoiceField(queryset=WorkerIdentity.objects.none(), label="Protected worker")
    credential_profile = forms.ChoiceField(label="Credential profile alias", help_text="Choose an alias advertised by the worker below. SSH keys stay on that worker.")
    host = forms.CharField(max_length=253, label="Login hostname")
    user = forms.CharField(max_length=80, label="Remote username")
    port = forms.IntegerField(min_value=1, max_value=65535, initial=22)
    remote_root = forms.CharField(max_length=240, label="Remote job directory")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["worker"].queryset = owned_workers(user)
        self.fields["credential_profile"].choices = sorted({(p["alias"], p["alias"]) for worker in owned_workers(user) for p in worker.profiles})
        workers = list(owned_workers(user))
        if len(workers) == 1:
            self.fields["worker"].initial = workers[0]

    def clean(self):
        data = super().clean()
        worker = data.get("worker")
        if worker:
            profile = next((p for p in worker.profiles if p.get("alias") == data.get("credential_profile")), None)
            if profile is None:
                self.add_error("credential_profile", "This worker has not advertised that profile.")
            else:
                data["profile"] = profile
                for field, key in (("host", "allowed_hosts"), ("user", "allowed_users"), ("port", "allowed_ports")):
                    if field in data and data[field] not in profile.get(key, []):
                        self.add_error(field, "This target is outside the worker's approved credential scope.")
                root = data.get("remote_root", "")
                if (not re.fullmatch(r"/[A-Za-z0-9_./-]+", root) or root == "/" or "//" in root
                        or any(p in {".", ".."} for p in root.split("/"))
                        or not any(root == p or root.startswith(p + "/") for p in profile.get("remote_root_prefixes", []))):
                    self.add_error("remote_root", "Choose a canonical directory under an approved remote prefix.")
        return data


class EnvironmentForm(forms.Form):
    name = forms.CharField(max_length=80, label="Environment name")
    connection = forms.ModelChoiceField(queryset=Connection.objects.none(), label="Cluster")
    kind = forms.ChoiceField(choices=[("system", "Existing Python / Bash"), ("modules", "Environment Modules / Lmod"), ("venv", "Existing Python virtual environment"), ("conda", "Existing Conda environment"), ("poetry", "Existing Poetry virtual environment")])
    interpreter = forms.CharField(max_length=240, initial="/usr/bin/python3", help_text="An existing executable on compute nodes. Module environments may use python3 through the loaded PATH.")
    discovered_modules = forms.MultipleChoiceField(required=False, choices=[], widget=forms.CheckboxSelectMultiple,
        label="Modules from this cluster", help_text="Saved discovery catalog; selected entries load first. No modules run while creating a recipe.")
    modules = forms.CharField(required=False, widget=forms.Textarea, label="Additional modules (advanced)",
        help_text="One exact module/version per line, appended after the selected catalog entries. For a specific load order, leave catalog entries unchecked and list all modules here in order.")
    module_init = forms.ChoiceField(required=False, choices=[], label="Module initialization (advanced)",
        help_text="Leave empty to use the cluster's module command in a login batch shell. An explicit file is used only if that command is missing.")
    module_paths = forms.CharField(required=False, max_length=4096, widget=forms.Textarea,
        label="Module search paths", help_text="Optional: one approved absolute directory per line. Prefilled from cluster discovery when available.")
    prefix = forms.CharField(required=False, max_length=240, label="Environment prefix")
    conda_executable = forms.CharField(required=False, max_length=240, label="Conda executable", help_text="Used only inside the reviewed compute job.")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["connection"].queryset = owned_connections(user)
        from compute_broker.backends.environments import MODULE_INIT_PATHS, module_init_path
        self.fields["module_init"].choices = [("", "Use the cluster-provided module command")] + [(p, p) for p in MODULE_INIT_PATHS]
        selected = self.data.get("connection") if self.is_bound else self.initial.get("connection")
        self.catalog_connection = None
        try:
            if selected:
                self.catalog_connection = owned_connections(user).filter(id=UUID(str(getattr(selected, "pk", selected)))).first()
        except (ValueError, TypeError, AttributeError):
            pass
        self.available_modules = []
        if self.catalog_connection:
            module_data = self.catalog_connection.discovery_snapshot.get("software", {}).get("modules", {})
            try:
                discovered_init = module_init_path(module_data.get("init_path"))
            except ValueError:
                pass
            else:
                if discovered_init not in dict(self.fields["module_init"].choices):
                    self.fields["module_init"].choices = [*self.fields["module_init"].choices, (discovered_init, discovered_init)]
            values = module_data.get("available", [])
            if isinstance(values, list):
                self.available_modules = list(dict.fromkeys(value for value in values if isinstance(value, str)
                    and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./+@:-]{0,159}", value)
                    and not any(part in {"", ".", ".."} for part in value.split("/"))))[:2000]
            self.fields["connection"].widget = forms.HiddenInput()
        self.fields["discovered_modules"].choices = [(name, name) for name in self.available_modules]

    def clean(self):
        data = super().clean()
        kind = data.get("kind")
        interpreter = data.get("interpreter", "")
        absolute = lambda value: bool(re.fullmatch(r"/[A-Za-z0-9_./-]+", value)) and not any(p in {".", ".."} for p in value.split("/"))
        if not absolute(interpreter) and not (kind == "modules" and interpreter in {"python3", "bash"}):
            self.add_error("interpreter", "Use an absolute existing interpreter, or python3/bash for a module environment.")
        manual_modules = [line.strip() for line in data.get("modules", "").splitlines() if line.strip()]
        modules = list(dict.fromkeys(data.get("discovered_modules", []) + manual_modules))
        if len(modules) > 32 or any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./+@:-]{0,159}", m) or ".." in m.split("/") for m in modules):
            self.add_error("modules", "Use up to 32 exact module/version names.")
        if kind == "modules" and not modules:
            self.add_error("modules", "Select at least one module.")
        if kind != "modules" and modules:
            self.add_error("modules", "Module loads belong to a module environment.")
        paths = [line.strip() for line in data.get("module_paths", "").splitlines() if line.strip()]
        if len(paths) > 16 or any(not absolute(p) or p == "/" or "//" in p or ":" in p for p in paths):
            self.add_error("module_paths", "Use up to 16 canonical absolute directory paths, one per line.")
        if kind != "modules" and paths:
            self.add_error("module_paths", "Module search paths belong to a module environment.")
        if kind == "conda":
            for field in ("prefix", "conda_executable"):
                if not absolute(data.get(field, "")):
                    self.add_error(field, "Provide an existing absolute path on the cluster.")
        data["module_names"] = modules
        data["module_path_list"] = paths
        return data
