"""Tests for cluster discovery."""
from compute_relay.control import services
from test_support.unit_test_control import broker

from test_support.unit_test_management import discovery_report, form_post, human, management, queued_discovery
import json
from datetime import timedelta
import pytest
from django.test import Client
from django.utils import timezone
from compute_relay.control.auth import token_hash
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Connection, WorkerIdentity
from test_support.unit_test_management import outstanding_discovery_attempt


def test_discovery_lease_is_worker_scoped_and_replay_is_rejected(management):
    from compute_relay.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    stranger = WorkerIdentity.objects.create(name="unrelated worker", owner=context.outsider,
                                             token_hash=token_hash("unrelated-discovery-worker"))
    assert lease_discovery(stranger) is None
    lease = lease_discovery(context.worker)["lease"]
    assert lease["operation"] == "discover"
    assert lease["connection_id"] == str(connection.id)
    report = discovery_report(connection, context.worker, lease)
    with pytest.raises(BrokerError) as denied:
        report_discovery(stranger, connection.id, report)
    assert denied.value.status == 404
    with pytest.raises(BrokerError) as stale:
        report_discovery(context.worker, connection.id, {**report, "generation": lease["generation"] - 1})
    assert stale.value.status == 409
    assert report_discovery(context.worker, connection.id, report)["accepted"] is True
    connection.refresh_from_db()
    assert connection.discovery_state == "ready"
    assert connection.discovery_snapshot["software"]["verified_on_compute"] is False
    with pytest.raises(BrokerError) as duplicate:
        report_discovery(context.worker, connection.id, report)
    assert duplicate.value.status == 409


def test_rediscovery_fences_old_lease_and_failure_preserves_last_good_snapshot(management):
    from compute_relay.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    connection.discovery_snapshot = {"last_good": "preserve this synthetic metadata"}
    connection.save(update_fields=["discovery_snapshot"])
    old = lease_discovery(context.worker)["lease"]
    response = form_post(human(context.owner), f"/clusters/{connection.id}/discover/")
    assert response.status_code == 302
    with pytest.raises(BrokerError) as stale:
        report_discovery(context.worker, connection.id, discovery_report(connection, context.worker, old))
    assert stale.value.status == 409
    new = lease_discovery(context.worker)["lease"]
    report_discovery(context.worker, connection.id, {"lease_id": new["id"], "generation": new["generation"],
                                                    "outcome": "discovery_failed", "error_code": "AUTH_REQUIRED"})
    connection.refresh_from_db()
    assert connection.discovery_state == "failed"
    assert connection.discovery_snapshot == {"last_good": "preserve this synthetic metadata"}
    assert connection.discovery_error == "AUTH_REQUIRED"


def test_discovery_report_rejects_secret_fields_and_changed_targets(management):
    from compute_relay.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    lease = lease_discovery(context.worker)["lease"]
    report = discovery_report(connection, context.worker, lease)
    bad_config = {**report["configuration"], "identity_file": "/private/worker/key"}
    with pytest.raises(BrokerError):
        report_discovery(context.worker, connection.id, {**report, "configuration": bad_config})
    changed = {**report["configuration"], "ssh": {"host": "different.invalid", "user": "fixtureuser", "port": 22}}
    with pytest.raises(BrokerError) as target:
        report_discovery(context.worker, connection.id, {**report, "configuration": changed})
    assert target.value.status == 409
    connection.refresh_from_db()
    assert connection.discovery_snapshot == {}


def test_expired_discovery_report_cannot_replace_last_good_snapshot(management):
    from compute_relay.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    connection.discovery_snapshot = {"last_good": True}
    connection.save(update_fields=["discovery_snapshot"])
    lease = lease_discovery(context.worker)["lease"]
    Connection.objects.filter(id=connection.id).update(discovery_lease_expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(BrokerError) as expired:
        report_discovery(context.worker, connection.id, discovery_report(connection, context.worker, lease))
    assert expired.value.status == 409
    connection.refresh_from_db()
    assert connection.discovery_snapshot == {"last_good": True}


def test_discovery_cannot_change_execution_profile_until_existing_accounting_settles(management):
    from compute_relay.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    initial = lease_discovery(context.worker)["lease"]
    report_discovery(context.worker, connection.id, discovery_report(connection, context.worker, initial))
    connection.refresh_from_db()
    original_revision, original_snapshot = connection.revision_sha256, connection.discovery_snapshot
    attempt = outstanding_discovery_attempt(context, connection)
    form_post(human(context.owner), f"/clusters/{connection.id}/discover/")
    lease = lease_discovery(context.worker)["lease"]
    context.worker.profiles[0]["revision_sha256"] = "b" * 64
    context.worker.profiles[0]["site_profile"] = {**context.worker.profiles[0]["site_profile"], "maximum_cpus": 1}
    context.worker.save(update_fields=["profiles"])
    report = discovery_report(connection, context.worker, lease, discovery_snapshot={"updated": True})
    with pytest.raises(BrokerError) as busy:
        report_discovery(context.worker, connection.id, report)
    assert busy.value.code == "CONNECTION_BUSY" and busy.value.status == 409
    connection.refresh_from_db()
    assert connection.revision_sha256 == original_revision
    assert connection.discovery_snapshot == original_snapshot
    attempt.accounting_state = "settled"
    attempt.save(update_fields=["accounting_state"])
    assert report_discovery(context.worker, connection.id, report)["accepted"]
    connection.refresh_from_db()
    assert connection.revision_sha256 != original_revision
    assert connection.discovery_snapshot == {"updated": True}


def test_discovery_can_refresh_snapshot_without_changing_unsettled_execution_profile(management):
    from compute_relay.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    initial = lease_discovery(context.worker)["lease"]
    report_discovery(context.worker, connection.id, discovery_report(connection, context.worker, initial))
    connection.refresh_from_db()
    original_revision = connection.revision_sha256
    outstanding_discovery_attempt(context, connection)
    form_post(human(context.owner), f"/clusters/{connection.id}/discover/")
    lease = lease_discovery(context.worker)["lease"]
    report = discovery_report(connection, context.worker, lease, discovery_snapshot={"new_inventory_only": True})
    assert report_discovery(context.worker, connection.id, report)["accepted"]
    connection.refresh_from_db()
    assert connection.revision_sha256 == original_revision
    assert connection.discovery_snapshot == {"new_inventory_only": True}


def test_profile_advertisement_requires_worker_and_rejects_secret_fields(management):
    context = management
    endpoint = "/internal/v1/worker/profiles"
    payload = {"profiles": [{**context.worker.profiles[0], "private_key": "SYNTHETIC_PRIVATE_KEY_SENTINEL"}]}
    agent = Client().post(endpoint, data=json.dumps(payload), content_type="application/json",
                          HTTP_AUTHORIZATION="Bearer " + context.token)
    assert agent.status_code == 401
    response = Client().post(endpoint, data=json.dumps(payload), content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer synthetic-management-worker")
    assert response.status_code == 400
    context.worker.refresh_from_db()
    assert "SYNTHETIC_PRIVATE_KEY_SENTINEL" not in json.dumps(context.worker.profiles)
    assert b"SYNTHETIC_PRIVATE_KEY_SENTINEL" not in response.content


def test_saved_discovery_is_escaped_when_rendered_in_cluster_portal(management):
    context = management
    connection = context.connections[0]
    payload = "<script>syntheticSnapshotAttack()</script>"
    connection.discovery_snapshot = {"notice": payload, "software": {"scope": "login_node", "verified_on_compute": False}}
    connection.save(update_fields=["discovery_snapshot"])
    response = human(context.owner).get(f"/clusters/{connection.id}/")
    assert response.status_code == 200
    assert payload.encode() not in response.content
    assert b"&lt;script&gt;" in response.content


def test_confirmed_personal_discovery_cannot_change_owner_site_policy(broker):
    from copy import deepcopy
    from uuid import uuid4
    from compute_relay.control.cluster_services import report_discovery
    from compute_relay.control.models import ClusterRegistration
    connection = broker.connection
    config = {"backend": "fake", "credential_profile": "personal", "ssh": {"host": "cluster.example", "user": "owner", "port": 22},
        "remote_root": "/scratch/owner", "profile_revision": "1", "credential_profile_revision_sha256": "a" * 64,
        "site_profile": {"automatic_execution_allowed": False, "maximum_cpus": 8}}
    connection.configuration = config
    connection.discovery_lease_id = uuid4()
    connection.discovery_generation = 1
    connection.discovery_lease_expires_at = timezone.now() + timedelta(minutes=2)
    connection.save()
    ClusterRegistration.objects.create(owner=broker.owner, connection=connection, name="personal", host="cluster.example")
    changed = deepcopy(config)
    changed["site_profile"]["automatic_execution_allowed"] = True
    broker.worker.profiles = [{"alias": "personal", "revision_sha256": "a" * 64, "site_profile": changed["site_profile"]}]
    broker.worker.save()
    with pytest.raises(BrokerError) as error:
        report_discovery(broker.worker, connection.id, {"lease_id": str(connection.discovery_lease_id), "generation": 1,
            "outcome": "discovered", "configuration": changed, "connection_revision_sha256": services.digest(changed), "discovery_snapshot": {}})
    assert error.value.code == "REVISION_CHANGED"
    connection.refresh_from_db()
    assert connection.configuration == config and connection.configuration_confirmed


@pytest.mark.parametrize("collection_state,pending", [("pending", True), ("retry_pending", True), ("failed", False)])
def test_rediscovery_preserves_binding_until_settled_outputs_are_recovered(management, collection_state, pending):
    from compute_relay.control.cluster_services import lease_discovery, report_discovery
    context = management
    connection = queued_discovery(context)
    first = lease_discovery(context.worker)["lease"]
    report_discovery(context.worker, connection.id, discovery_report(connection, context.worker, first))
    connection.refresh_from_db()
    original_revision = connection.revision_sha256
    attempt = outstanding_discovery_attempt(context, connection)
    attempt.accounting_state, attempt.state = "settled", "succeeded"
    attempt.native_identity = {"job_id": "123"}
    attempt.collection_state, attempt.collection_pending = collection_state, pending
    attempt.save()
    form_post(human(context.owner), f"/clusters/{connection.id}/discover/")
    lease = lease_discovery(context.worker)["lease"]
    context.worker.profiles[0]["revision_sha256"] = "b" * 64
    context.worker.save(update_fields=["profiles"])
    report = discovery_report(connection, context.worker, lease, discovery_snapshot={"updated": True})
    with pytest.raises(BrokerError) as denied:
        report_discovery(context.worker, connection.id, report)
    assert denied.value.code == "CONNECTION_BUSY"
    connection.refresh_from_db()
    assert connection.revision_sha256 == original_revision
    attempt.collection_pending, attempt.collection_state = False, "complete"
    attempt.save()
    assert report_discovery(context.worker, connection.id, report)["accepted"]
