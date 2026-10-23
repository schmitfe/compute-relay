"""Human CPU allocation settings keep saved discovery separate from authority."""

from copy import deepcopy
from html.parser import HTMLParser
from types import SimpleNamespace
from uuid import uuid4

from django.template.loader import render_to_string
import pytest

from compute_broker.control import cluster_configuration as configuration
from test_cluster_configuration import form_data, snapshot


@pytest.fixture
def allocation_candidate(monkeypatch):
    derive = configuration.derive_cluster_configuration

    def use(value=2, *, status="known"):
        candidate = {"status": status, "value": value, "basis": "saved_scheduler_topology",
            "partitions": [{"name": "cpu", "unit": value, "reason": "Saved logical CPU topology"}],
            "note": "Provisional until confirmed by the cluster owner."}

        def evidence(snapshot, current):
            return {**derive(snapshot, current), "allocation_candidates": {"cpu_allocation_quantum": deepcopy(candidate)}}

        monkeypatch.setattr(configuration, "derive_cluster_configuration", evidence)
        return candidate

    return use


def test_known_candidate_prefills_the_field_but_old_post_omission_does_not_apply_it(snapshot, allocation_candidate):
    allocation_candidate()
    form = configuration.ClusterConfigurationForm(snapshot=snapshot)
    assert form.initial["cpu_allocation_quantum"] == 2
    fields = [field.name for field in form.resource_fields]
    assert fields[fields.index("maximum_cpus") + 1] == "cpu_allocation_quantum"
    omitted = configuration.ClusterConfigurationForm(form_data(), snapshot=snapshot)
    assert omitted.is_valid(), omitted.errors
    assert "cpu_allocation_quantum" not in omitted.site_profile()


def test_explicit_saved_unit_wins_over_discovery_and_is_preserved_on_old_post_omission(snapshot, allocation_candidate):
    allocation_candidate(2)
    current = {"cpu_allocation_quantum": 4, "allocated_cpus": 3, "allocated_gpus": 0}
    form = configuration.ClusterConfigurationForm(snapshot=snapshot, current_profile=current)
    assert form.initial["cpu_allocation_quantum"] == 4
    omitted = configuration.ClusterConfigurationForm(form_data(), snapshot=snapshot, current_profile=current)
    assert omitted.is_valid(), omitted.errors
    assert omitted.site_profile()["cpu_allocation_quantum"] == 4
    assert omitted.site_profile()["allocated_cpus"] == 3


def test_owner_can_save_change_or_explicitly_clear_the_unit_without_enabling_automatic_execution(snapshot, allocation_candidate):
    allocation_candidate(2)
    chosen = configuration.ClusterConfigurationForm(form_data(cpu_allocation_quantum=2), snapshot=snapshot)
    assert chosen.is_valid(), chosen.errors
    assert chosen.site_profile()["cpu_allocation_quantum"] == 2
    assert chosen.site_profile()["automatic_execution_allowed"] is False
    changed = configuration.ClusterConfigurationForm(form_data(cpu_allocation_quantum=4), snapshot=snapshot,
        current_profile=chosen.site_profile())
    assert changed.is_valid(), changed.errors
    assert changed.site_profile()["cpu_allocation_quantum"] == 4
    cleared = configuration.ClusterConfigurationForm(form_data(cpu_allocation_quantum=""), snapshot=snapshot,
        current_profile=chosen.site_profile())
    assert cleared.is_valid(), cleared.errors
    assert "cpu_allocation_quantum" not in cleared.site_profile()
    assert cleared.site_profile()["maximum_time_limit_seconds"] == chosen.site_profile()["maximum_time_limit_seconds"]
    assert cleared.site_profile()["automatic_execution_allowed"] is False


@pytest.mark.parametrize("value,status", [(None, "unknown"), (2, "unknown"), (None, "known"), (True, "known")])
def test_unknown_or_malformed_candidates_leave_an_unspecified_unit_blank(snapshot, allocation_candidate, value, status):
    allocation_candidate(value, status=status)
    form = configuration.ClusterConfigurationForm(snapshot=snapshot)
    assert form.initial["cpu_allocation_quantum"] is None
    assert form.cpu_allocation_candidate_known is False


@pytest.mark.parametrize("unit", [0, -1, 1025, "not-a-number", "1.5"])
def test_cpu_unit_requires_a_supported_integer_with_field_attached_errors(snapshot, unit):
    form = configuration.ClusterConfigurationForm(form_data(cpu_allocation_quantum=unit), snapshot=snapshot)
    assert not form.is_valid() and "cpu_allocation_quantum" in form.errors


def test_one_unit_must_fit_the_cpu_ceiling(snapshot):
    form = configuration.ClusterConfigurationForm(form_data(maximum_cpus=3, cpu_allocation_quantum=4), snapshot=snapshot)
    assert not form.is_valid()
    assert "One CPU allocation unit" in str(form.errors["cpu_allocation_quantum"])


def test_retained_minimum_floor_is_rounded_up_before_checking_the_cpu_ceiling(snapshot):
    form = configuration.ClusterConfigurationForm(form_data(maximum_cpus=3, cpu_allocation_quantum=2),
        snapshot=snapshot, current_profile={"allocated_cpus": 3})
    assert not form.is_valid()
    assert "rounds to 4 logical CPUs" in str(form.errors["cpu_allocation_quantum"])
    fits = configuration.ClusterConfigurationForm(form_data(maximum_cpus=4, cpu_allocation_quantum=2),
        snapshot=snapshot, current_profile={"allocated_cpus": 3})
    assert fits.is_valid(), fits.errors
    assert fits.site_profile()["allocated_cpus"] == 3 and fits.site_profile()["cpu_allocation_quantum"] == 2


def rendered(form, *, cpu_allocation_ready=True):
    return render_to_string("broker/cluster_configuration.html", {
        "form": form, "cluster": SimpleNamespace(id=uuid4(), name="Synthetic cluster", configuration_confirmed=True,
            revision_sha256="a" * 64), "connector_ready": True, "remote_root": "/synthetic/jobs",
        "cpu_allocation_ready": cpu_allocation_ready,
    })


def test_configuration_template_explains_provisional_known_candidate_next_to_its_field(snapshot, allocation_candidate):
    allocation_candidate()
    text = rendered(configuration.ClusterConfigurationForm(snapshot=snapshot))
    assert 'name="cpu_allocation_quantum"' in text and "CPU allocation unit (logical CPUs)" in text
    assert "request for 3 logical CPUs allocates 4" in text
    assert "Saved discovery suggests a CPU allocation unit" in text
    assert "provisional scheduler metadata, not verification by a compute job" in text
    assert "CPU allocation evidence by partition" in text
    assert text.index("Saved discovery suggests") < text.index('name="maximum_gpus"')


def test_configuration_template_shows_unknown_candidate_without_claiming_verification(snapshot, allocation_candidate):
    allocation_candidate(None, status="unknown")
    text = rendered(configuration.ClusterConfigurationForm(snapshot=snapshot))
    assert "Saved discovery could not establish a CPU allocation unit" in text
    assert "Resource visibility alone does not verify allocation rounding" in text


@pytest.mark.parametrize("ready", [False, True])
def test_cpu_unit_editing_requires_updated_connector_without_disabling_other_settings(snapshot, allocation_candidate, ready):
    allocation_candidate()
    text = rendered(configuration.ClusterConfigurationForm(snapshot=snapshot), cpu_allocation_ready=ready)

    class Inputs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.fieldsets = []
            self.disabled = {}

        def handle_starttag(self, tag, attributes):
            attrs = dict(attributes)
            if tag == "fieldset":
                self.fieldsets.append("disabled" in attrs)
            if tag == "input" and attrs.get("name"):
                self.disabled[attrs["name"]] = "disabled" in attrs or any(self.fieldsets)

        def handle_endtag(self, tag):
            if tag == "fieldset":
                self.fieldsets.pop()

    inputs = Inputs()
    inputs.feed(text)
    assert inputs.disabled["cpu_allocation_quantum"] is (not ready)
    assert inputs.disabled["maximum_cpus"] is False
    assert inputs.disabled["allow_automatic_execution"] is False
    assert "Saved discovery suggests a CPU allocation unit" in text
    assert ("Restart this cluster's personal connector" in text) is (not ready)
