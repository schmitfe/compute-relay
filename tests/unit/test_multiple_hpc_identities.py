"""One local owner may use independent remote identities on the same host."""

from copy import deepcopy
from datetime import timedelta

from django.test import Client
from django.utils import timezone
import pytest

from compute_relay.control.auth import connection_allowed, token_hash
from compute_relay.control.connection_identity import public_ssh_identity
from compute_relay.control.forms import ProjectForm
from compute_relay.control.models import (ClusterRegistration, Connection, Environment, Grant,
    Project, ProjectCredential, WorkerIdentity)
from compute_relay.control.services import digest
from test_cluster_setup import registration_data
from test_management import management, human, form_post


def test_same_cluster_host_retains_distinct_remote_users_workers_accounts_and_key_scope(management):
    owner = management.owner
    client = human(owner)
    connections, environments = [], []
    recipe = {"kind": "system", "interpreter": "/usr/bin/python3", "modules": []}
    recipe_revision = digest(recipe)
    for index, remote_user in enumerate(("researcher", "service"), start=1):
        name = "University"
        response = form_post(client, "/clusters/new/", registration_data(
            name=name, host="university.invalid", user=remote_user, remote_root=""))
        assert response.status_code == 302
        registration = ClusterRegistration.objects.get(owner=owner, name=name, remote_user=remote_user)
        alias = "personal-" + registration.id.hex
        revision = str(index) * 64
        site = {"partitions": ["cpu"], "accounts": [f"allocation-{remote_user}"]}
        worker = WorkerIdentity.objects.create(owner=owner, name=f"Connector for {remote_user}",
            token_hash=token_hash("synthetic-worker-" + remote_user), profiles=[{
                "alias": alias, "backend": "slurm", "revision_sha256": revision,
                "allowed_hosts": ["university.invalid"], "allowed_users": [remote_user], "allowed_ports": [22],
                "remote_root_prefixes": [f"/home/{remote_user}/relay"], "site_profile": site}])
        configuration = {"backend": "slurm", "credential_profile": alias,
            "credential_profile_revision_sha256": revision,
            "ssh": {"host": "university.invalid", "user": remote_user, "port": 22},
            "remote_root": f"/home/{remote_user}/relay", "site_profile": site}
        connection = Connection.objects.create(owner=owner, worker=worker, name=name, backend="slurm",
            configuration=configuration, revision_sha256=digest(configuration))
        registration.connection = connection
        registration.save(update_fields=["connection"])
        environment = Environment.objects.create(owner=owner, connection=connection, name="Python for " + remote_user,
            configuration=recipe, revision_sha256=recipe_revision)
        connections.append(connection)
        environments.append(environment)

    assert ClusterRegistration.objects.filter(owner=owner, host="university.invalid").count() == 2
    assert connections[0].worker_id != connections[1].worker_id
    assert connections[0].id != connections[1].id
    assert {connection.owner_id for connection in connections} == {owner.id}
    labels = {str(connection) for connection in connections}
    assert labels == {"University (researcher@university.invalid)", "University (service@university.invalid)"}
    choices = {str(value): str(label) for value, label in ProjectForm(user=owner).fields["connections"].choices}
    assert {choices[str(connection.id)] for connection in connections} == labels
    cluster_page = client.get("/clusters/").content.decode()
    assert all(label in cluster_page for label in labels)

    project = Project.objects.create(owner=owner, name="Both university identities")
    project.connections.set(connections)
    project.environments.set(environments)
    policy = deepcopy(management.grant.policy)
    policy.update(project_id=str(project.id), allowed_connection_ids=[str(c.id) for c in connections],
        allowed_environment_revision_sha256=[recipe_revision])
    Grant.objects.create(project=project, policy=policy)
    token = "synthetic-multiple-hpc-identities"
    key = ProjectCredential.objects.create(project=project, issued_by=owner, name="One project agent",
        token_hash=token_hash(token), scopes=["jobs:read"], expires_at=timezone.now() + timedelta(days=1))
    assert all(connection_allowed(key, connection.id) for connection in connections)
    agent = Client(HTTP_AUTHORIZATION="Bearer " + token)
    inventory = agent.get("/api/v1/connections").json()
    assert {target["id"] for target in inventory["connections"]} == {str(c.id) for c in connections}
    assert {tuple(sorted(target["ssh_identity"].items())) for target in inventory["connections"]} == {
        tuple(sorted({"host": "university.invalid", "user": remote_user, "port": 22}.items()))
        for remote_user in ("researcher", "service")}
    assert {tuple(target["capabilities"]["accounts"]) for target in inventory["connections"]} == {
        ("allocation-researcher",), ("allocation-service",)}
    assert {environment["connection_id"] for environment in inventory["environments"]} == {str(c.id) for c in connections}

    key.allowed_connection_ids = [str(connections[1].id)]
    key.save(update_fields=["allowed_connection_ids"])
    inventory = agent.get("/api/v1/connections").json()
    assert [target["id"] for target in inventory["connections"]] == [str(connections[1].id)]
    assert [environment["id"] for environment in inventory["environments"]] == [str(environments[1].id)]
    assert not connection_allowed(key, connections[0].id)


@pytest.mark.parametrize("changes", [
    {"host": "/private/key"}, {"host": "-option"}, {"user": "$(command)"},
    {"port": True}, {"port": "22"}, {"port": 0},
])
def test_public_login_identity_rejects_invalid_fields_without_exposing_private_settings(changes):
    configuration = {"ssh": {"host": "cluster.invalid", "user": "researcher", "port": 22,
        "identity_file": "/private/never-publish", **changes}}
    assert public_ssh_identity(configuration) is None


def test_login_identity_whitelists_only_public_fields_and_fake_labels_stay_simple():
    configuration = {"ssh": {"host": "cluster.invalid", "user": "researcher", "port": 2222,
        "identity_file": "/private/never-publish", "known_hosts": "/private/hosts"}}
    assert public_ssh_identity(configuration) == {"host": "cluster.invalid", "user": "researcher", "port": 2222}
    assert str(Connection(name="University", backend="slurm", configuration=configuration)) == "University (researcher@cluster.invalid:2222)"
    assert str(Connection(name="Simulator", backend="fake", configuration=configuration)) == "Simulator"
