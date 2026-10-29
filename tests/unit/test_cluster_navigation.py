"""Cluster configuration pages keep their owner-approved parent navigation."""

import pytest
from django.utils.html import escape

from compute_relay.control.models import ClusterRegistration, ConnectorTask
from test_support.unit_test_connector_setup import personal
from test_support.unit_test_management import human, management


pytestmark = pytest.mark.django_db


def cluster_links(response):
    return response.content.decode().split('<nav aria-label="Cluster sections">', 1)[1].split("</nav>", 1)[0]


@pytest.mark.parametrize("suffix,current", [
    ("", "Cluster"),
    ("environments/", "Environments"),
    ("environments/new/", "Add environment"),
    ("configuration/", "Cluster configuration"),
    ("directories/", "Working directory"),
    ("test/", "Test cluster"),
])
def test_personal_cluster_pages_keep_context_and_configuration_shortcuts(personal, suffix, current):
    base = f"/clusters/{personal.connection.id}/"
    cluster_name = escape(str(personal.connection))
    response = personal.human.get(base + suffix)
    assert response.status_code == 200
    html = response.content.decode()
    assert '<nav class="breadcrumbs" aria-label="Breadcrumb"><ol>' in html
    assert f'<span aria-current="page">{current if suffix else cluster_name}</span>' in html
    for target in ("", "environments/", "configuration/", "directories/", "test/"):
        assert f'href="{base}{target}"' in cluster_links(response)
    if suffix:
        assert f'<a href="{base}">{cluster_name}</a>' in html
        assert f'<a href="{base}">Back to cluster</a>' in html
    if suffix == "environments/new/":
        assert f'<a href="{base}environments/">Back to environments</a>' in html
    if suffix == "directories/":
        assert f'<a href="{base}configuration/">Back to cluster configuration</a>' in html
    assert not ConnectorTask.objects.exists()


@pytest.mark.parametrize("suffix", ["", "environments/", "environments/new/", "test/"])
def test_managed_cluster_navigation_does_not_offer_personal_configuration(management, suffix):
    base = f"/clusters/{management.connections[0].id}/"
    response = human(management.owner).get(base + suffix)
    assert response.status_code == 200
    links = cluster_links(response)
    assert f'href="{base}configuration/"' not in links
    assert f'href="{base}directories/"' not in links
    assert f'href="{base}environments/"' in links


@pytest.mark.parametrize("suffix,current", [("", "Draft cluster"), ("edit/", "Edit cluster details"), ("remove/", "Remove cluster")])
def test_draft_configuration_keeps_setup_parent(management, suffix, current):
    draft = ClusterRegistration.objects.create(owner=management.owner, name="Draft cluster", host="draft.example.invalid")
    base = f"/clusters/setup/{draft.id}/"
    response = human(management.owner).get(base + suffix)
    assert response.status_code == 200
    html = response.content.decode()
    assert f'<span aria-current="page">{current}</span>' in html
    if suffix:
        assert f'<a href="{base}">Draft cluster</a>' in html
        assert f'<a href="{base}">Back to cluster setup</a>' in html
    if suffix == "remove/":
        assert f'<a href="{base}">Cancel</a>' in html
    draft.refresh_from_db()
    assert not draft.archived


@pytest.mark.parametrize("route", ["connection", "registration"])
def test_remove_cancel_returns_to_cluster_even_when_opened_through_setup(personal, route):
    base = f"/clusters/{personal.connection.id}/"
    path = base if route == "connection" else f"/clusters/setup/{personal.registration.id}/"
    response = personal.human.get(path + "remove/?next=https://example.invalid/")
    assert response.status_code == 200
    assert f'<a href="{base}">Cancel</a>'.encode() in response.content
    assert b"https://example.invalid/" not in response.content
    assert f'href="{base}configuration/"' in cluster_links(response)
    personal.connection.refresh_from_db()
    assert not personal.connection.archived
