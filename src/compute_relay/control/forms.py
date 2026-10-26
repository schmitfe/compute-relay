"""Human management forms. No form accepts an SSH key or agent-supplied shell."""

import re
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR
from uuid import UUID, uuid4

from django import forms
from django.utils import timezone

from .models import Connection, Environment

SCOPES = [(value, label) for value, label in (
    ("jobs:read", "Read jobs and logs"), ("objects:write", "Upload input files"),
    ("jobs:write", "Prepare jobs"), ("jobs:execute", "Request execution"),
    ("jobs:cancel", "Request cancellation"), ("artifacts:read", "Download results"))]
ALL_SCOPES = [key for key, _ in SCOPES]


def owned_connections(user):
    rows = Connection.objects.filter(archived=False)
    return rows.filter(owner=user)


def owned_environments(user):
    return Environment.objects.filter(owner=user)




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
    max_active_jobs = forms.IntegerField(min_value=1, max_value=1000, initial=2, label="Concurrent jobs",
        help_text="Up to 1,000 active jobs per project; cluster scheduling and account limits still apply. This is separate from the connector's bounded SSH command concurrency.")
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
        self.fields["environments"].help_text = "Recipes belong to their cluster. Save cluster changes to load its available recipes."
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
        from compute_relay.extensions import metadata_items
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




class EnvironmentForm(forms.Form):
    name = forms.CharField(max_length=80, label="Environment name")
    connection = forms.ModelChoiceField(queryset=Connection.objects.none(), label="Cluster")
    kind = forms.ChoiceField(choices=[("system", "Existing Python / Bash"), ("modules", "Environment Modules / Lmod"), ("venv", "Existing Python virtual environment"), ("conda", "Conda / Anaconda / Miniforge environment"), ("poetry", "Existing Poetry virtual environment")])
    detected_interpreter = forms.ChoiceField(required=False, choices=[], label="Detected Python", help_text="Choose a saved or inspected executable, or keep the manual interpreter below.")
    detected_environment = forms.ChoiceField(required=False, choices=[], label="Detected environment", help_text="Existing Conda or Poetry/venv prefixes on this cluster. Select one to fill its kind, prefix and interpreter.")
    interpreter = forms.CharField(max_length=4096, initial="/usr/bin/python3", help_text="An existing executable on compute nodes. Module environments may use python3 through the loaded PATH.")
    discovered_modules = forms.MultipleChoiceField(required=False, choices=[], widget=forms.CheckboxSelectMultiple,
        label="Modules from this cluster", help_text="Saved discovery catalog; selected entries load first. Inspect selection explicitly loads these modules on the login node; saving a recipe does not.")
    modules = forms.CharField(required=False, max_length=8192, widget=forms.Textarea, label="Additional modules (advanced)",
        help_text="One exact module/version per line, appended after the selected catalog entries. For a specific load order, leave catalog entries unchecked and list all modules here in order.")
    module_init = forms.ChoiceField(required=False, choices=[], label="Module initialization (advanced)",
        help_text="Leave empty to use the cluster's module command in a login batch shell. An explicit file is used only if that command is missing.")
    module_paths = forms.CharField(required=False, max_length=4096, widget=forms.Textarea,
        label="Module search paths", help_text="Optional: one approved absolute directory per line. Prefilled from cluster discovery when available.")
    prefix = forms.CharField(required=False, max_length=4096, label="Environment prefix")
    conda_executable = forms.CharField(required=False, max_length=4096, label="Conda executable", help_text="Leave empty during inspection to locate Conda through the selected modules. Used by conda run inside compute jobs.")

    def __init__(self, *args, user, inspection=False, observation=None, probe_selection=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.inspection, self.probe_selection = inspection, probe_selection
        if inspection:
            self.fields["name"].required = False
            self.fields["interpreter"].required = False
        self.observation = observation
        self.fields["connection"].queryset = owned_connections(user)
        from compute_relay.backends.environments import MODULE_INIT_PATHS, module_init_path
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
        software = self.catalog_connection.discovery_snapshot.get("software", {}) if self.catalog_connection else {}
        python = observation["python"] if observation else [item.get("interpreter") for item in software.get("python", []) if isinstance(item, dict)]
        self.detected_environments = {}
        for item in (observation["environments"] if observation else software.get("environments", [])):
            if isinstance(item, dict) and item.get("kind") in {"conda", "venv", "poetry"} and isinstance(item.get("prefix"), str) and isinstance(item.get("interpreter"), str):
                self.detected_environments[item["prefix"]] = item
        self.fields["detected_interpreter"].choices = [("", "Use manual interpreter")] + [(path, path) for path in dict.fromkeys(path for path in python if isinstance(path, str))]
        self.fields["detected_environment"].choices = [("", "Use manual environment paths")] + [(prefix, item["kind"] + ": " + prefix) for prefix, item in self.detected_environments.items()]
        self.order_fields(["connection", "name", "kind", "discovered_modules", "modules", "module_init", "module_paths",
                           "detected_interpreter", "detected_environment", "interpreter", "prefix", "conda_executable"])

    def clean(self):
        data = super().clean()
        if not self.inspection:
            if data.get("detected_interpreter"):
                data["interpreter"] = data["detected_interpreter"]
            if data.get("detected_environment"):
                chosen = self.detected_environments[data["detected_environment"]]
                data.update({key: chosen[key] for key in ("kind", "prefix", "interpreter")})
                if chosen["kind"] == "conda" and self.observation:
                    data["conda_executable"] = self.observation["conda_executable"]
                elif chosen["kind"] == "conda" and not data.get("conda_executable") and self.catalog_connection:
                    data["conda_executable"] = self.catalog_connection.discovery_snapshot.get("software", {}).get("conda", {}).get("executable", "")
                elif chosen["kind"] != "conda":
                    data["conda_executable"] = ""
        manual_modules = [line.strip() for line in data.get("modules", "").splitlines() if line.strip()]
        modules = list(dict.fromkeys(data.get("discovered_modules", []) + manual_modules))
        if data.get("kind") == "system" and modules:
            data["kind"] = "modules"
            # The initial system path must not bypass the selected module's
            # Python. Keep explicitly detected/custom executable paths intact.
            if not data.get("detected_interpreter") and data.get("interpreter") in {"/usr/bin/python3", "/usr/bin/bash"}:
                data["interpreter"] = data["interpreter"].rsplit("/", 1)[-1]
        kind = data.get("kind")
        interpreter = data.get("interpreter", "")
        from compute_relay.backends.environments import absolute_environment_path
        def absolute(value):
            try:
                absolute_environment_path(value)
                return True
            except ValueError:
                return False
        if (not self.inspection or interpreter) and not absolute(interpreter) and not (kind == "modules" and interpreter in {"python3", "bash"}):
            self.add_error("interpreter", "Use an absolute existing interpreter, or python3/bash for a module environment.")
        if len(modules) > 32 or any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./+@:-]{0,159}", m) or ".." in m.split("/") for m in modules):
            self.add_error("modules", "Use up to 32 exact module/version names.")
        if kind == "modules" and not modules:
            self.add_error("modules", "Select at least one module.")
        paths = [line.strip() for line in data.get("module_paths", "").splitlines() if line.strip()]
        if len(paths) > 16 or any(not absolute(p) or p == "/" or "//" in p or ":" in p for p in paths):
            self.add_error("module_paths", "Use up to 16 canonical absolute directory paths, one per line.")
        if kind == "system" and paths:
            self.add_error("module_paths", "Module search paths belong to a module environment.")
        if kind == "conda" and not self.inspection:
            for field in ("prefix", "conda_executable"):
                if not absolute(data.get(field, "")):
                    self.add_error(field, "Provide an existing absolute path on the cluster.")
        data["module_names"] = modules
        data["module_path_list"] = paths
        for field in ("prefix", "conda_executable"):
            if data.get(field) and not absolute(data[field]):
                self.add_error(field, "Use a canonical absolute path on the cluster.")
        if not self.inspection and self.probe_selection is not None and (data.get("detected_interpreter") or data.get("detected_environment")):
            from .environment_inspection import module_selection, selection
            if module_selection(selection(data)) != module_selection(self.probe_selection):
                self.add_error("discovered_modules", "The module selection changed. Inspect it again before using these detected paths, or select manual paths.")
        return data
