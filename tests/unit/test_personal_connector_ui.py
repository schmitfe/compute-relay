"""Personal setup hints and owner-only UI; no SSH or credential files are read."""

from unittest.mock import patch

import pytest

from compute_relay.control.cluster_setup import RegistrationForm, owned_registrations
from compute_relay.control.models import ClusterRegistration, User
from test_support.unit_test_management import management, human, form_post
from test_support.unit_test_cluster_setup import registration_data


@pytest.mark.parametrize("field", ["identity_file", "known_hosts"])
@pytest.mark.parametrize("value", ["~/.ssh/private file", "/private/keys/cluster.key"])
def test_local_path_hints_are_saved_as_paths_without_reading_them(field, value):
    with patch("builtins.open", side_effect=AssertionError("a local hint must not be read by the portal")):
        form = RegistrationForm(registration_data(**{field: value}))
        assert form.is_valid(), form.errors
    assert form.cleaned_data["connector_settings"] == {field: value, "session_seconds": 28800}


@pytest.mark.parametrize("field", ["identity_file", "known_hosts"])
@pytest.mark.parametrize("value", ["relative/key", "~other/.ssh/key", "-i key", "/key\n-oBad", "/key\r", "/key\x00", "\n/key"])
def test_local_hints_reject_relative_option_and_control_character_values(field, value):
    form = RegistrationForm(registration_data(**{field: value}))
    assert not form.is_valid() and field in form.errors


@pytest.mark.parametrize("value, expected", [("", 28800), (1, 60), (1440, 86400)])
def test_optional_session_lifetime_is_finite(value, expected):
    form = RegistrationForm(registration_data(session_minutes=value, password="never-save", token="never-save"))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["connector_settings"] == {"session_seconds": expected}


@pytest.mark.parametrize("value", [0, 1441, "not-an-integer", "28.5"])
def test_invalid_session_lifetime_is_rejected(value):
    form = RegistrationForm(registration_data(session_minutes=value))
    assert not form.is_valid() and "session_minutes" in form.errors


def test_advanced_ssh_validation_is_visible_without_reading_keys(management):
    client = human(management.owner)
    before = client.get("/clusters/new/")
    assert b'<summary>Advanced SSH settings and directory hint</summary>' in before.content
    assert b'<details open><summary>Advanced SSH' not in before.content
    response = form_post(client, "/clusters/new/", registration_data(identity_file="relative/key"))
    assert response.status_code == 200
    assert b'<details open><summary>Advanced SSH' in response.content
    assert b'href="#field_identity_file"' in response.content
    assert not ClusterRegistration.objects.exists()


def test_registration_create_edit_and_prefill_retain_only_connector_hints(management):
    client = human(management.owner)
    response = form_post(client, "/clusters/new/", registration_data(backend="slurm",
        identity_file="~/.ssh/science", known_hosts="/private/known-hosts", session_minutes=15,
        password="not-retained", worker_token="not-retained"))
    assert response.status_code == 302
    setup = ClusterRegistration.objects.get(owner=management.owner)
    assert setup.connector_settings == {"identity_file": "~/.ssh/science", "known_hosts": "/private/known-hosts", "session_seconds": 900}
    assert setup.connection_id is None
    edit = client.get(f"/clusters/setup/{setup.id}/edit/")
    assert edit.context["form"].initial["identity_file"] == "~/.ssh/science"
    assert edit.context["form"].initial["session_minutes"] == 15
    changed = form_post(client, f"/clusters/setup/{setup.id}/edit/", registration_data(
        backend="slurm", setup_revision=setup.revision, identity_file="/private/second-key", session_minutes=20))
    assert changed.status_code == 302
    setup.refresh_from_db()
    assert setup.connector_settings == {"identity_file": "/private/second-key", "session_seconds": 1200}


def test_personal_setup_is_primary_with_revision_and_csrf(management):
    client = human(management.owner)
    response = form_post(client, "/clusters/new/", registration_data(backend="slurm"))
    setup = ClusterRegistration.objects.get(owner=management.owner)
    page = client.get(response.url)
    body = page.content.decode()
    assert f'action="/clusters/setup/{setup.id}/connect/"' in body
    assert f'name="setup_revision" value="{setup.revision}"' in body
    assert 'name="csrfmiddlewaretoken"' in body
    assert "Advanced: configure" not in body and "/workers/" not in body
    assert "compute-relay connector --setup" in body
    assert "No compute job is submitted during setup" in body


def test_incomplete_setup_does_not_offer_personal_download(management):
    client = human(management.owner)
    response = form_post(client, "/clusters/new/", registration_data(backend="slurm", user=""))
    page = client.get(response.url)
    assert page.status_code == 200
    assert b"Download personal connector setup" not in page.content
    assert b"Advanced: configure" not in page.content


def test_simulators_are_not_offered_in_personal_cluster_setup(management):
    client = human(management.owner)
    response = form_post(client, "/clusters/new/", registration_data(backend="fake"))
    assert response.status_code == 200 and "Only Slurm" in str(response.context["form"].non_field_errors())
    assert not ClusterRegistration.objects.filter(backend="fake").exists()


def test_admin_cannot_view_or_modify_another_owners_personal_registration(management):
    setup = ClusterRegistration.objects.create(owner=management.owner, name="owner-only-setup", backend="slurm",
        host="fixture.invalid", remote_user="fixtureuser", requested_root="/fixture/jobs")
    admin = User.objects.create_superuser(username="personal-setup-admin", password=None)
    assert not owned_registrations(admin).filter(id=setup.id).exists()
    client = human(admin)
    for suffix in ["", "edit/"]:
        assert client.get(f"/clusters/setup/{setup.id}/{suffix}").status_code == 404
    assert form_post(client, f"/clusters/setup/{setup.id}/edit/", registration_data(setup_revision=setup.revision)).status_code == 404
    assert setup.name.encode() not in client.get("/clusters/").content


def test_bound_owned_slurm_cluster_offers_personal_setup_with_registration_revision(management):
    connection = management.connections[0]
    connection.backend = "slurm"
    connection.save(update_fields=["backend"])
    setup = ClusterRegistration.objects.create(owner=management.owner, name=connection.name, backend="slurm",
        host="fixture.invalid", remote_user="fixtureuser", requested_root="/fixture/jobs", connection=connection, revision=4)
    page = human(management.owner).get(f"/clusters/{connection.id}/")
    assert page.status_code == 200
    assert page.context["personal_setup"] == setup
    assert f'action="/clusters/{connection.id}/connect/"'.encode() in page.content
    assert b'name="setup_revision" value="4"' in page.content


def test_owned_slurm_connection_offers_personal_setup_without_registration(management):
    connection = management.connections[0]
    connection.backend = "slurm"
    connection.save(update_fields=["backend"])
    page = human(management.owner).get(f"/clusters/{connection.id}/")
    assert page.status_code == 200 and page.context["personal_setup"] is None
    assert f'action="/clusters/{connection.id}/connect/"'.encode() in page.content
    assert b'name="setup_revision" value=""' in page.content


def test_foreign_cluster_inspection_is_denied_even_to_admin(management):
    connection = management.connections[0]
    connection.backend = "slurm"
    connection.save(update_fields=["backend"])
    admin = User.objects.create_superuser(username="personal-inspection-admin", password=None)
    page = human(admin).get(f"/clusters/{connection.id}/")
    assert page.status_code == 404
    assert b"Download personal connector setup" not in page.content
