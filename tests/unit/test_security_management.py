"""Security regressions for explicit resource ownership and discovery revision fences."""

import copy
from types import SimpleNamespace

import pytest

from compute_relay.control.auth import token_hash
from compute_relay.control.cluster_services import lease_discovery, report_discovery
from compute_relay.control.errors import BrokerError
from compute_relay.control.forms import owned_environments
from compute_relay.control.models import (
    Attempt, Connection, Environment, ExecutionRequest, Grant, Job, Project, User, WorkerIdentity,
)
from compute_relay.control.services import digest


@pytest.fixture
def resources(db):
    alice = User.objects.create_user(username="alice-resource-owner", password=None)
    bob = User.objects.create_user(username="bob-resource-user", password=None)
    profile = {"alias": "alice-private", "backend": "fake", "revision_sha256": "a" * 64,
               "allowed_hosts": ["simulation.invalid"], "allowed_users": ["alice"], "allowed_ports": [22],
               "remote_root_prefixes": ["/simulation/alice"], "site_profile": {}}
    worker = WorkerIdentity.objects.create(owner=alice, name="Alice's protected worker",
                                            token_hash=token_hash("synthetic-private-worker"), profiles=[profile])
    configuration = {"backend": "fake", "credential_profile": "alice-private",
                     "ssh": {"host": "simulation.invalid", "user": "alice", "port": 22},
                     "remote_root": "/simulation/alice/jobs", "profile_revision": 1,
                     "credential_profile_revision_sha256": profile["revision_sha256"], "site_profile": {}}
    # A historical cross-owner connection must not delegate the entire worker.
    connection = Connection.objects.create(owner=bob, worker=worker, name="Historical connection",
                                            backend="fake", configuration=configuration,
                                            revision_sha256=digest(configuration))
    return SimpleNamespace(alice=alice, bob=bob, worker=worker, connection=connection,
                           profile=profile, configuration=configuration)






def test_project_or_connection_link_cannot_override_explicit_environment_owner(resources):
    environment = Environment.objects.create(owner=resources.alice, connection=resources.connection,
                                              name="Alice's environment", configuration={}, revision_sha256="e" * 64)
    project = Project.objects.create(owner=resources.bob, name="Bob's project")
    project.environments.add(environment)
    assert owned_environments(resources.alice).filter(id=environment.id).exists()
    assert not owned_environments(resources.bob).filter(id=environment.id).exists()


def _report(resources, lease, configuration, snapshot):
    return report_discovery(resources.worker, resources.connection.id,
                            {"lease_id": lease["id"], "generation": lease["generation"],
                             "outcome": "discovered", "configuration": configuration,
                             "connection_revision_sha256": digest(configuration), "discovery_snapshot": snapshot})


def _discovery_lease(resources):
    Connection.objects.filter(id=resources.connection.id).update(
        discovery_state="queued", discovery_lease_id=None, discovery_lease_expires_at=None)
    lease = lease_discovery(resources.worker)["lease"]
    assert lease["operation"] == "discover"
    return lease


def test_discovery_can_refresh_snapshot_but_cannot_rebind_unsettled_jobs(resources):
    project = Project.objects.create(owner=resources.alice, name="Active job project")
    grant = Grant.objects.create(project=project, policy={})
    job = Job.objects.create(project=project, request={}, manifest={})
    execution = ExecutionRequest.objects.create(job=job)
    # A completed job awaiting accounting is still unsettled and needs its exact
    # original worker/configuration binding for collection and reconciliation.
    attempt = Attempt.objects.create(execution_request=execution, job=job, connection=resources.connection,
                                     grant=grant, state="succeeded", accounting_state="settlement_pending",
                                     reservation_cpu=60, reservation_gpu=0)
    original = copy.deepcopy(resources.configuration)
    lease = _discovery_lease(resources)
    response = _report(resources, lease, original, {"version": "same-revision-refresh"})
    assert response["accepted"] is True
    resources.connection.refresh_from_db()
    assert resources.connection.discovery_snapshot == {"version": "same-revision-refresh"}

    updated = copy.deepcopy(original)
    updated["site_profile"] = {"maximum_cpus": 30}
    updated["credential_profile_revision_sha256"] = "b" * 64
    resources.profile.update(site_profile=updated["site_profile"], revision_sha256="b" * 64)
    resources.worker.profiles = [resources.profile]
    resources.worker.save(update_fields=["profiles"])
    lease = _discovery_lease(resources)
    with pytest.raises(BrokerError) as error:
        _report(resources, lease, updated, {"version": "new-revision"})
    assert error.value.code == "CONNECTION_BUSY"
    resources.connection.refresh_from_db()
    assert resources.connection.revision_sha256 == digest(original)
    assert resources.connection.configuration == original
    assert resources.connection.discovery_snapshot == {"version": "same-revision-refresh"}

    attempt.accounting_state = "settled"
    attempt.save(update_fields=["accounting_state"])
    assert _report(resources, lease, updated, {"version": "new-revision"})["accepted"] is True
    resources.connection.refresh_from_db()
    assert resources.connection.revision_sha256 == digest(updated)
