from django import forms

from compute_relay.control.models import Environment

from .rendering import validate_policy


class BootstrapChoices(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.connection.name}: {obj.name}"


class BuildPolicyForm(forms.Form):
    enabled = forms.BooleanField(required=False, label="Allow agents to request environment builds")
    allowed_bases = BootstrapChoices(queryset=Environment.objects.none(), required=False,
        label="Approved starting environments", help_text="Choose standalone system Python environments from this project. Module, Conda and virtual-environment bootstraps are not supported yet; normal execution policy must also permit the selection.")
    cpus = forms.IntegerField(min_value=1, max_value=1024, label="Maximum CPU cores per build")
    memory_mib = forms.IntegerField(min_value=128, max_value=16777216, label="Maximum memory per build (MiB)")
    time_limit_minutes = forms.IntegerField(min_value=1, max_value=10080, label="Maximum runtime per build (minutes)")
    platform_tag = forms.CharField(max_length=80, label="Expected compute platform", help_text="For example linux-x86_64. Confirm on the compute platform; login-node discovery is only a candidate.")
    python_major_minor = forms.CharField(max_length=5, label="Expected Python version", help_text="Major and minor version, for example 3.13.")
    max_environment_bytes = forms.IntegerField(min_value=0, max_value=2**53 - 1, label="Maximum built environment size (bytes)")
    max_environment_files = forms.IntegerField(min_value=0, max_value=10000000, label="Maximum built files and directories")
    minimum_free_bytes = forms.IntegerField(min_value=0, max_value=2**53 - 1, label="Keep at least this much free space (bytes)",
        help_text="Advisory check before installation; this does not enforce a filesystem quota.")
    minimum_free_files = forms.IntegerField(min_value=0, max_value=10000000, label="Keep at least this many free filesystem entries")

    def __init__(self, *args, project, configuration=None, **kwargs):
        configuration = configuration or {"enabled": False, "allowed_base_environment_ids": [], "resource_limits": {
            "cpus": 1, "memory_mib": 1024, "time_limit_seconds": 600}, "platform_tag": "linux-x86_64",
            "python_major_minor": "3.13", "max_environment_bytes": 1073741824, "max_environment_files": 20000,
            "minimum_free_bytes": 1073741824, "minimum_free_files": 10000}
        initial = {key: configuration.get(key, 0) for key in ("enabled", "platform_tag", "python_major_minor", "max_environment_bytes",
            "max_environment_files", "minimum_free_bytes", "minimum_free_files")}
        initial.update(allowed_bases=configuration["allowed_base_environment_ids"], cpus=configuration["resource_limits"]["cpus"],
            memory_mib=configuration["resource_limits"]["memory_mib"], time_limit_minutes=configuration["resource_limits"]["time_limit_seconds"] // 60)
        super().__init__(*args, initial=initial, **kwargs)
        self.fields["allowed_bases"].queryset = project.environments.filter(connection_id__isnull=False).select_related("connection")

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        configuration = {key: data[key] for key in ("enabled", "platform_tag", "python_major_minor", "max_environment_bytes",
            "max_environment_files", "minimum_free_bytes", "minimum_free_files")}
        configuration.update(allowed_base_environment_ids=[str(value.id) for value in data["allowed_bases"]],
            allowed_managers=["venv"], allowed_sources=["uploaded_wheels"], resource_limits={"cpus": data["cpus"],
            "memory_mib": data["memory_mib"], "time_limit_seconds": data["time_limit_minutes"] * 60})
        try:
            validate_policy(configuration)
        except (ValueError, TypeError, KeyError) as exc:
            raise forms.ValidationError(str(exc)) from exc
        data["configuration"] = configuration
        return data
