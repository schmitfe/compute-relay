"""Human resource settings form; authentication and worker application are external."""

from copy import deepcopy

from django import forms

from compute_broker.domain.cluster_configuration import (
    ALLOWLIST_FIELDS, RESOURCE_BOUNDS, ClusterConfigurationError,
    derive_cluster_configuration, validate_cluster_configuration,
)


class ClusterConfigurationForm(forms.Form):
    allow_automatic_execution = forms.BooleanField(required=False,
        label="Allow automatic execution on this cluster",
        help_text="When unchecked, this cluster requires human confirmation for every execution, even in an automatic project. Checking it removes only this cluster's confirmation requirement; project policy, each API key's confirmation preference, resource limits and compute allowance still apply.")
    module_availability_enabled = forms.BooleanField(required=False, label="Also run module avail during discovery",
        help_text="Run the site's module listing through your worker, using system initialization and discovered search paths. This may evaluate site module configuration under your HPC account. No module is loaded and no compute job is submitted.")
    partitions = forms.CharField(max_length=81000, help_text="Comma-separated partition names. Discovery is visibility, not permission.")
    accounts = forms.CharField(max_length=81000, required=False, help_text="Comma-separated account names. Confirm access with your site.")
    qos = forms.CharField(max_length=81000, required=False, label="QoS", help_text="Optional comma-separated job QoS names; partition QoS is separate.")
    maximum_cpus = forms.IntegerField(min_value=1, max_value=1024, label="Maximum CPUs per job")
    cpu_allocation_quantum = forms.IntegerField(required=False, min_value=1, max_value=1024,
        label="CPU allocation unit (logical CPUs)",
        help_text="Effective CPUs = ceil(max(requested CPUs, minimum allocation) / unit) × unit. With unit 2, a request for 3 logical CPUs allocates 4. This changes CPU allocation and its reservation, not walltime or execution confirmation. Leave blank for no explicit unit.")
    maximum_gpus = forms.IntegerField(min_value=0, max_value=16, label="Maximum GPUs of one type per job")
    maximum_memory_mib = forms.IntegerField(min_value=128, max_value=16777216, label="Maximum memory per job (MiB)")
    maximum_time_limit_seconds = forms.IntegerField(min_value=60, max_value=604800, label="Maximum walltime (seconds)",
        help_text="Use a whole number of minutes. Unknown discovery limits require an explicit finite value.")

    def __init__(self, *args, snapshot, current_profile=None, **kwargs):
        self.snapshot = snapshot
        self.current_profile = validate_cluster_configuration(current_profile or {}, require_limits=False)
        self.evidence = derive_cluster_configuration(snapshot, self.current_profile)
        initial = {field: ", ".join(self.evidence["site_profile"].get(field, [])) for field in ALLOWLIST_FIELDS}
        initial.update({field: self.evidence["site_profile"].get(field) for field in RESOURCE_BOUNDS})
        candidate = self.evidence.get("allocation_candidates", {}).get("cpu_allocation_quantum", {})
        self.cpu_allocation_candidate = candidate if isinstance(candidate, dict) else {}
        value = self.cpu_allocation_candidate.get("value")
        candidate_known = (self.cpu_allocation_candidate.get("status") == "known"
            and type(value) is int and 1 <= value <= 1024)
        self.cpu_allocation_candidate_known = candidate_known
        self.cpu_allocation_unit_saved = "cpu_allocation_quantum" in self.current_profile
        initial["cpu_allocation_quantum"] = self.current_profile.get("cpu_allocation_quantum",
            value if candidate_known else None)
        initial["allow_automatic_execution"] = self.current_profile.get("automatic_execution_allowed") is True
        initial.update(kwargs.pop("initial", {}))
        super().__init__(*args, initial=initial, **kwargs)

    @property
    def resource_fields(self):
        fields = [*ALLOWLIST_FIELDS, *RESOURCE_BOUNDS]
        fields.insert(fields.index("maximum_cpus") + 1, "cpu_allocation_quantum")
        return [self[field] for field in fields]

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        profile = dict(self.current_profile)
        for field in ALLOWLIST_FIELDS:
            values = [value.strip() for value in data[field].split(",") if value.strip()]
            profile[field] = values
        for field in RESOURCE_BOUNDS:
            profile[field] = data[field]
        unit = data.get("cpu_allocation_quantum") if "cpu_allocation_quantum" in self.data else self.current_profile.get("cpu_allocation_quantum")
        if unit is None:
            profile.pop("cpu_allocation_quantum", None)
        else:
            profile["cpu_allocation_quantum"] = data["cpu_allocation_quantum"] = unit
            minimum = max(1, self.current_profile.get("allocated_cpus", 1))
            rounded_minimum = ((minimum + unit - 1) // unit) * unit
            if unit > data["maximum_cpus"]:
                self.add_error("cpu_allocation_quantum", "One CPU allocation unit must fit within Maximum CPUs per job.")
            elif rounded_minimum > data["maximum_cpus"]:
                self.add_error("cpu_allocation_quantum", f"The retained minimum CPU allocation rounds to {rounded_minimum} logical CPUs. Increase Maximum CPUs per job or choose a unit that fits.")
        if self.errors:
            return data
        profile["automatic_execution_allowed"] = data["allow_automatic_execution"]
        try:
            profile = validate_cluster_configuration(profile)
        except ClusterConfigurationError as exc:
            self.add_error(None, str(exc))
            return data
        # Only the selected partitions constrain the known physical ceilings.
        selected = [item for item in self.evidence["partitions"] if item["name"] in profile["partitions"]]
        for field in RESOURCE_BOUNDS:
            ceiling = max((item[field] for item in selected if item[field] is not None), default=None)
            if ceiling is not None and profile[field] > ceiling:
                self.add_error(field, "This exceeds the saved discovered ceiling for the selected partitions.")
        if not self.errors:
            self._site_profile = profile
        return data

    def site_profile(self):
        if not self.is_valid():
            raise ValueError("Validate the cluster configuration form before applying it.")
        return deepcopy(self._site_profile)
