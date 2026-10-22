"""Deterministic old-worker discovery versus personal connector re-pairing."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from threading import Event
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection, transaction
from django.test import Client

from compute_broker.control import cluster_services
from compute_broker.control.identity.providers import DEFAULT_PROVIDERS
from compute_broker.control.models import ClusterRegistration, Connection, User, WorkerIdentity
from compute_broker.worker.enrollment import profile_metadata


@pytest.mark.django_db(transaction=True)
def test_repair_waits_for_old_worker_before_locking_discovery_connection(settings):
    """A discovery holding Worker must never meet a re-pair holding Connection.

    The SQL wrapper releases the discovery exactly when re-pair attempts to
    lock or revoke the old Worker row. With the old Connection→Worker order,
    re-pair already owns Connection and this schedule forms a real PostgreSQL
    deadlock. With Worker→Connection, discovery completes before re-pair moves
    the connection and revokes the old identity.
    """
    if connection.vendor != "postgresql":
        pytest.skip("Requires the PostgreSQL integration job")
    settings.BROKER_IDENTITY_PROVIDERS = deepcopy(DEFAULT_PROVIDERS)
    owner = User.objects.create_user(username="connector-lock-" + uuid4().hex[:12])
    registration = ClusterRegistration.objects.create(owner=owner, name="Synthetic lock fixture", host="cluster.example.invalid",
        remote_user="synthetic", requested_root="/srv/broker/jobs")
    human = Client();human.force_login(owner)
    profile = profile_metadata("personal-lock-fixture", {"backend": "slurm",
        "allowed_hosts": [registration.host], "allowed_users": [registration.remote_user], "allowed_ports": [registration.port],
        "remote_root_prefixes": [registration.requested_root], "site_profile": {"automatic_execution_allowed": False},
        "ssh": {"mode": "mfa", "control_path": "/private/fixture/s", "known_hosts": "/private/fixture/known_hosts"}})

    def download():
        registration.refresh_from_db()
        response = human.post(f"/clusters/setup/{registration.id}/connect/", {"setup_revision": registration.revision})
        assert response.status_code == 200
        return response.json()["pairing_token"]

    def pair(token):
        return Client().post("/internal/v1/connectors/enroll", json.dumps({"profile": profile}),
            content_type="application/json", HTTP_AUTHORIZATION="Bearer " + token)

    initial = pair(download())
    assert initial.status_code == 201
    connection_id, old_worker_id = initial.json()["connection_id"], initial.json()["worker_id"]
    token = download()
    old_worker = WorkerIdentity.objects.get(id=old_worker_id)
    lease = cluster_services.lease_discovery(old_worker)["lease"]
    report = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "discovery_failed",
        "error_code": "SYNTHETIC_DISCOVERY_UNAVAILABLE"}
    worker_locked, pairing_waiting_on_worker = Event(), Event()

    def observer():
        close_old_connections()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '8s'")
                    cursor.execute("SET LOCAL statement_timeout = '12s'")
                current = WorkerIdentity.objects.select_for_update().get(id=old_worker_id)
                worker_locked.set()
                assert pairing_waiting_on_worker.wait(5), "Pairing never attempted its old-worker authority lock"
                return cluster_services.report_discovery(current, connection_id, report)
        finally:
            close_old_connections()

    def replacement():
        close_old_connections()
        try:
            def synchronize(execute, sql, parameters, many, context):
                statement = sql.upper()
                matches_old_worker = any(str(value) == old_worker_id for value in (parameters or ()))
                if ("BROKER_WORKERIDENTITY" in statement and matches_old_worker
                        and ("FOR UPDATE" in statement or "FOR NO KEY UPDATE" in statement or statement.lstrip().startswith("UPDATE"))):
                    pairing_waiting_on_worker.set()
                return execute(sql, parameters, many, context)
            with transaction.atomic(), connection.execute_wrapper(synchronize):
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '8s'")
                    cursor.execute("SET LOCAL statement_timeout = '12s'")
                response = pair(token)
                return response.status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        discovery = executor.submit(observer)
        assert worker_locked.wait(5), "Discovery did not acquire its old-worker authority lock"
        repair = executor.submit(replacement)
        assert discovery.result(timeout=20) == {"accepted": True, "state": "failed"}
        assert repair.result(timeout=20) == 201

    current = Connection.objects.get(pk=connection_id)
    assert str(current.worker_id) != old_worker_id
    assert current.discovery_state == "queued" and current.discovery_lease_id is None
    assert WorkerIdentity.objects.get(pk=old_worker_id).revoked
    assert Connection.objects.count() == 1 and WorkerIdentity.objects.count() == 2
