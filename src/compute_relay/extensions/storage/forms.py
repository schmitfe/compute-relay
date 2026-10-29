"""Human-only protected roots or personal-connector directory selections."""

from decimal import Decimal, ROUND_FLOOR
from uuid import UUID, uuid4

from django import forms

from compute_relay.control.models import Connection
from .models import StorageCapacityPool
from .planning import ROLES, SAFE_INTEGER, StoragePlanningError, canonical_root
from .services import protected_roots, selected_targets, target_current

GIB = Decimal(1024**3)


class StorageTargetForm(forms.Form):
    name = forms.CharField(max_length=100, label="Location name")
    connection = forms.ModelChoiceField(queryset=Connection.objects.none(), label="Cluster")
    root_alias = forms.ChoiceField(choices=[], required=False, label="Operator-approved directory")
    root_path = forms.CharField(max_length=240, required=False, label="Directory on this cluster",
        help_text="Choose a dedicated directory. Missing folders in this path will be created using your cluster account. Existing contents and permissions are preserved.")
    roles = forms.MultipleChoiceField(choices=[(role, role.title()) for role in ROLES], initial=list(ROLES),
        required=False, widget=forms.CheckboxSelectMultiple, label="Use this location for")
    connection_revision = forms.CharField(required=False, widget=forms.HiddenInput)
    submission_id = forms.UUIDField(initial=uuid4, required=False, widget=forms.HiddenInput)
    pool = forms.ModelChoiceField(queryset=StorageCapacityPool.objects.none(), required=False, label="Shared capacity pool",
        help_text="Leave blank for a new pool. Group locations only when they use the same backing filesystem; identity remains unverified without a protected observation.")
    enable_probes = forms.BooleanField(required=False, initial=False, label="Enable read-only capacity checks",
        help_text="An enabled storage worker may log in and read free byte/inode statistics once per minute. No directory scan, file contents, writes or deletion.")

    def __init__(self, *args, user, project, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.fields["connection"].queryset = project.connections.filter(owner=user, enabled=True, archived=False)
        value = self.data.get("connection") if self.is_bound else self.initial.get("connection")
        if not self.is_bound and not value:
            choices = list(self.fields["connection"].queryset[:2])
            if len(choices) == 1:
                value = choices[0].id
                self.initial["connection"] = value
        self.selected_connection = None
        self.personal = False
        try:
            self.selected_connection = self.fields["connection"].queryset.select_related("worker").filter(id=UUID(str(getattr(value, "id", value)))).first()
        except (ValueError, TypeError, AttributeError):
            pass
        if self.selected_connection:
            self.fields["connection"].widget = forms.HiddenInput()
            from compute_relay.control.connector_setup import personal
            self.personal = personal(self.selected_connection)
            roots = protected_roots(self.selected_connection)
            self.fields["root_alias"].choices = [(item["alias"], f"{item['alias']} — {item['path']}") for item in roots]
            self.fields["pool"].queryset = StorageCapacityPool.objects.filter(owner_id=self.selected_connection.owner_id,
                connection=self.selected_connection, enabled=True, storagetarget__project=project).distinct()
            self.initial.setdefault("connection_revision", self.selected_connection.revision_sha256)
        if self.personal:
            self.fields["root_alias"].widget = forms.HiddenInput()
            self.fields["pool"].widget = forms.HiddenInput()
            self.fields["enable_probes"].disabled = True
            self.fields["enable_probes"].widget = forms.HiddenInput()
            self.initial["enable_probes"] = False
            self.fields["enable_probes"].help_text = "Unavailable for a personal directory selection. Advisory project estimates work without probes; filesystem identity, free capacity and native quota remain unknown."
        else:
            for field in ("root_path", "roles", "connection_revision", "submission_id"):
                self.fields[field].widget = forms.HiddenInput()
        self.fields["root_alias"].help_text = "" if self.personal else "Only current roots advertised by this cluster's protected worker are available. No directory is scanned or created."

    def clean(self):
        data = super().clean()
        if self.personal:
            try:
                canonical_root(data.get("root_path"))
            except StoragePlanningError:
                self.add_error("root_path", "Choose a canonical absolute dedicated directory using letters, digits, dots, underscores, slashes or hyphens.")
            if not data.get("roles"):
                self.add_error("roles", "Choose at least one planning use.")
            if data.get("pool"):
                self.add_error("pool", "Personal directories use a separate unverified capacity pool.")
            if not data.get("submission_id"):
                self.add_error("submission_id", "Reload this form before continuing.")
        elif self.selected_connection and not data.get("root_alias"):
            self.add_error("root_alias", "Choose an operator-approved directory.")
        return data


class ProjectStorageForm(forms.Form):
    budget_gib = forms.DecimalField(required=False, min_value=0, max_value=Decimal(SAFE_INTEGER) / GIB, decimal_places=3,
        label="Shared project storage budget (GiB)", help_text="Advisory across all selected locations and API keys. Blank means no project byte budget.")
    inodes_budget = forms.IntegerField(required=False, min_value=0, max_value=SAFE_INTEGER,
        label="Shared project file/directory budget", help_text="An inode estimate, not a filesystem-enforced quota.")
    retained_gib_estimate = forms.DecimalField(min_value=0, max_value=Decimal(SAFE_INTEGER) / GIB, decimal_places=3, initial=0,
        label="Current retained data estimate (GiB)", help_text="Manual starting estimate. No scan has measured this value.")
    retained_inodes_estimate = forms.IntegerField(min_value=0, max_value=SAFE_INTEGER, initial=0,
        label="Current retained file/directory estimate")

    def __init__(self, *args, project, connection, **kwargs):
        super().__init__(*args, **kwargs)
        candidates = [target for target in selected_targets(project, connection) if target_current(target)]
        for role in ROLES:
            query = selected_targets(project, connection).filter(id__in=[target.id for target in candidates if role in target.roles])
            self.fields[role] = forms.ModelChoiceField(queryset=query, required=False,
                label={"data": "Input data location", "work": "Work and staging location", "environments": "Reusable environment location", "results": "Retained result location", "scratch": "Temporary scratch location"}[role])

    def clean(self):
        data = super().clean()
        for source, destination in (("budget_gib", "bytes_budget"), ("retained_gib_estimate", "retained_bytes_estimate")):
            value = data.get(source)
            data[destination] = int((value * GIB).to_integral_value(rounding=ROUND_FLOOR)) if value is not None else None
        return data
