"""The portal has one personal connector setup path and no manual worker UI."""

from uuid import uuid4

import pytest

from compute_relay.control.models import ClusterRegistration, Connection
from test_cluster_setup import registration_data
from test_management import management, human, form_post


@pytest.mark.parametrize("path", [
    "/workers/new/", "/workers/{id}/", "/workers/{id}/remove/",
    "/clusters/setup/{id}/bind/", "/clusters/setup/{id}/worker-config/",
    "/environments/", "/environments/new/",
])
def test_removed_management_routes_are_unavailable(management, path):
    client = human(management.owner)
    location = path.format(id=uuid4())
    assert client.get(location).status_code == 404
    assert form_post(client, location, {}).status_code == 404


def test_worker_fields_cannot_enroll_an_executable_cluster_from_the_portal(management):
    before = Connection.objects.count()
    response = form_post(human(management.owner), "/clusters/new/", registration_data(
        worker=str(management.worker.id), credential_profile="synthetic-profile"))
    assert response.status_code == 302
    registration = ClusterRegistration.objects.get(name="New science cluster")
    assert registration.backend == "slurm" and registration.connection_id is None
    assert Connection.objects.count() == before


def test_cluster_pages_offer_only_personal_setup_and_cluster_scoped_recipes(management):
    client = human(management.owner)
    response = form_post(client, "/clusters/new/", registration_data())
    setup = client.get(response.url)
    clusters = client.get("/clusters/")
    for page in (setup, clusters):
        assert page.status_code == 200
        assert b"/workers/" not in page.content
        assert b"worker-config/" not in page.content and b"/bind/" not in page.content
    assert b"Download personal connector setup" in setup.content
