"""Human forms select approved identifiers; no arbitrary remote path field."""

from decimal import Decimal, ROUND_FLOOR
from uuid import UUID

from django import forms

from compute_broker.control.forms import owned_connections
from compute_broker.control.models import Connection
from .models import StorageCapacityPool
from .planning import ROLES, SAFE_INTEGER
from .services import protected_roots, selected_targets, target_current

GIB = Decimal(1024**3)


class StorageTargetForm(forms.Form):
    name = forms.CharField(max_length=100, label="Location name")
    connection = forms.ModelChoiceField(queryset=Connection.objects.none(), label="Cluster")
    root_alias = forms.ChoiceField(choices=[], label="Operator-approved directory")
    pool = forms.ModelChoiceField(queryset=StorageCapacityPool.objects.none(), required=False, label="Shared capacity pool",
        help_text="Leave blank for a new pool. Group locations only when they use the same backing filesystem; identity remains unverified without a protected observation.")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["connection"].queryset = owned_connections(user).filter(enabled=True)
        value = self.data.get("connection") if self.is_bound else self.initial.get("connection")
        self.selected_connection = None
        try:
            self.selected_connection = self.fields["connection"].queryset.select_related("worker").filter(id=UUID(str(getattr(value, "id", value)))).first()
        except (ValueError, TypeError, AttributeError):
            pass
        if self.selected_connection:
            roots = protected_roots(self.selected_connection)
            self.fields["root_alias"].choices = [(item["alias"], f"{item['alias']} — {item['path']}") for item in roots]
            self.fields["pool"].queryset = StorageCapacityPool.objects.filter(owner_id=self.selected_connection.owner_id,
                connection=self.selected_connection, enabled=True)
        self.fields["root_alias"].help_text = "Only current roots advertised by this cluster's protected worker are available. No directory is scanned or created."


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
                label={"work": "Work and staging location", "environments": "Reusable environment location", "results": "Retained result location", "scratch": "Temporary scratch location"}[role])

    def clean(self):
        data = super().clean()
        for source, destination in (("budget_gib", "bytes_budget"), ("retained_gib_estimate", "retained_bytes_estimate")):
            value = data.get(source)
            data[destination] = int((value * GIB).to_integral_value(rounding=ROUND_FLOOR)) if value is not None else None
        return data
