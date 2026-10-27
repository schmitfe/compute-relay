"""Regression cases from the two October review rounds."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Attempt, StoragePool, UploadReservation
from compute_relay.control.storage import begin_transfer, blob_path, transfer_chunk
from compute_relay.backends.preparation import sha256
from test_backends import observation, ATTEMPT
from test_control import broker, budgeted, dispatched, execution, prepared_job, submit_receipt


def test_preempted_no_requeue_job_has_final_accounting():
    row = f"42|PREEMPTED|10|8|cpu=8|0:15|2026-10-21T12:00:00|2026-10-21T12:00:01|2026-10-21T12:00:11|broker-attempt:{ATTEMPT}|/managed/original-attempt|\n"
    result = observation(row)
    assert result["terminal"] and result["state"] == "failed"
    assert result["accounting"] == {"final": True, "cpu_core_seconds": 80, "gpu_device_seconds": 0}


def test_bad_transfer_releases_capacity_and_can_restart(broker, settings):
    binding = "upload:" + str(broker.credential.id)
    first = begin_transfer(broker.project, sha256(b"ok"), 2, binding, uploaded=True)
    with pytest.raises(BrokerError, match="checksum"):
        transfer_chunk(broker.project, first["transfer_id"], binding, 0, b"no", finish=True, uploaded=True)
    row = UploadReservation.objects.get(id=first["transfer_id"])
    broker.project.refresh_from_db()
    assert row.state == "aborted" and broker.project.storage_reserved == 0
    assert StoragePool.objects.get(id=1).reserved == 0
    assert not (settings.ARTIFACT_ROOT / str(broker.project.id) / ("upload-" + str(row.id))).exists()
    restarted = begin_transfer(broker.project, sha256(b"ok"), 2, binding, uploaded=True)
    assert restarted["offset"] == 0 and restarted["transfer_id"] != first["transfer_id"]
    assert transfer_chunk(broker.project, restarted["transfer_id"], binding, 0, b"ok", finish=True, uploaded=True)["complete"]


@pytest.mark.parametrize("failure", [FileNotFoundError(), BrokerError("OBJECT_INTEGRITY", "corrupt", 409),
    RuntimeError("Unexpected extension failure")])
def test_bad_first_candidate_does_not_starve_worker(broker, failure):
    budgeted(broker)
    broken_job, healthy_job = prepared_job(broker), prepared_job(broker)
    broken = execution(broker, broken_job).attempt
    healthy = execution(broker, healthy_job).attempt
    missing = broken_job.files.get(path="main.py").blob_id
    original = services.read_blob
    def read(blob):
        if blob.id == missing:
            raise failure
        return original(blob)
    # These jobs deliberately share content; make only the first candidate fail.
    original_lease = services._lease_attempt
    def lease(attempt, entries, now):
        if attempt.id == broken.id:
            with patch.object(services, "read_blob", side_effect=read):
                return original_lease(attempt, entries, now)
        return original_lease(attempt, entries, now)
    with patch.object(services, "_lease_attempt", side_effect=lease):
        result = services.lease_work(broker.worker)
    assert result["lease"]["attempt_id"] == str(healthy.id)
    broken.refresh_from_db()
    assert broken.error_code and broken.next_poll_at > timezone.now()
    assert broken.lease_expires_at is None and broken.generation == 0


@pytest.mark.parametrize("cancel", [False, True])
def test_management_lease_does_not_read_inputs(broker, cancel):
    budgeted(broker)
    attempt = execution(broker, prepared_job(broker)).attempt
    Attempt.objects.filter(id=attempt.id).update(dispatch_started=True, native_identity={"job_id": "17"}, cancel_requested=cancel)
    with patch.object(services, "read_blob", side_effect=FileNotFoundError()):
        lease = services.lease_work(broker.worker)["lease"]
    assert lease["operation"] == ("cancel" if cancel else "observe")
    assert lease["prepared"]["files"] == {}


@pytest.mark.parametrize("fields", [
    {"error_code": None}, {"observation": []}, {"observation": {"accounting": []}},
    {"observation": {"state": "completed", "terminal": True, "accounting": {"final": True}}},
])
def test_invalid_report_is_client_error_and_preserves_lease(broker, fields):
    import json
    budgeted(broker)
    attempt, lease = dispatched(broker, prepared_job(broker))
    native, lease = submit_receipt(broker, attempt, lease)
    data = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "observed",
        "native_identity": native, "observation": {"state": "running"}, **fields}
    response = Client(HTTP_AUTHORIZATION="Bearer synthetic-worker-token").post(
        f"/internal/v1/attempts/{attempt.id}/report", data=json.dumps(data), content_type="application/json")
    assert response.status_code == 400
    attempt.refresh_from_db()
    assert attempt.lease_expires_at is not None and attempt.accounting_state == "reserved"


def test_transfer_hashes_once_and_cached_verification_detects_changes(broker):
    from compute_relay.control import storage
    content = b"original bytes"
    first = begin_transfer(broker.project, sha256(content), len(content), "test")
    with patch.object(storage, "fingerprint", wraps=storage.fingerprint) as hashed:
        transfer_chunk(broker.project, first["transfer_id"], "test", 0, content, finish=True)
        blob = storage.Blob.objects.get(project=broker.project, sha256=sha256(content))
        storage.verify_blob(blob)
        storage.verify_blob(blob)
        assert hashed.call_count == 1
        blob_path(blob).write_bytes(b"changed! bytes")
        with pytest.raises(BrokerError, match="verification"):
            storage.verify_blob(blob)


def test_default_execution_key_survives_facade_restart():
    from compute_relay.mcp.facade import ProjectFacade
    from test_mcp import ClientSpy
    client = ClientSpy()
    ProjectFacade(client, client.project_id).request_execution(client.job_id)
    ProjectFacade(client, client.project_id).request_execution(client.job_id)
    assert client.calls[0][3]["idempotency_key"] == client.calls[1][3]["idempotency_key"]
    ProjectFacade(client, client.project_id).request_execution(client.job_id, "deliberate-repeat")
    assert client.calls[-1][3]["idempotency_key"] == "deliberate-repeat"


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


def test_helper_bundle_is_streamed_before_request(tmp_path):
    import json
    import shlex
    import subprocess
    import sys
    from compute_relay.transports import OpenSSHTransport
    transport = OpenSSHTransport({"python_path": sys.executable, "ssh": {
        "host": "cluster.example", "user": "owner", "known_hosts": str(tmp_path / "known_hosts")}})
    source = 'import sys,json\n# ' + "x" * 150000 + '\nprint(json.dumps({"request":json.load(sys.stdin),"unicode":"ä"}))\n'
    argv, payload = transport._helper_command(source, {"input": "\\quoted"})
    assert len(argv[-1]) < 512 and "\n" not in argv[-1] and "\\" not in argv[-1]
    result = subprocess.run(shlex.split(argv[-1]), input=payload, capture_output=True, check=True)
    assert json.loads(result.stdout) == {"request": {"input": "\\quoted"}, "unicode": "ä"}


def test_compacted_keys_do_not_exhaust_active_key_quota(broker, settings):
    from compute_relay.control.metadata import compact_idempotency
    settings.MAX_PROJECT_IDEMPOTENCY_KEYS = 1
    action = lambda: ({"ok": True}, 200)
    services.idempotent(broker.project, "test", "first", {}, action)
    compact_idempotency(before=timezone.now() + timedelta(seconds=1))
    assert services.idempotent(broker.project, "test", "second", {}, action)[1] == 200
    assert services.idempotent(broker.project, "test", "first", {}, action)[0]["error"]["code"] == "IDEMPOTENCY_RETIRED"


def test_review_exposes_invisible_characters_and_omitted_files(broker):
    from compute_relay.control.storage import store_uploaded_blob
    from compute_relay.control.models import Blob, JobFile
    job = prepared_job(broker)
    blob = store_uploaded_blob(broker.project, "print('safe') #\u202eevil\u2066\u200b\ufeff\n".encode())
    job.files.filter(path="main.py").update(blob=blob)
    oversized = Blob.objects.create(project=broker.project, sha256="f" * 64, size_bytes=1024 * 1024 + 1)
    JobFile.objects.create(job=job, path="helper.py", blob=oversized)
    client = Client();client.force_login(broker.owner)
    page = client.get(f"/review/{job.id}/")
    assert page.status_code == 200
    assert all(f"U+{code}".encode() in page.content for code in ("202E", "2066", "200B", "FEFF"))
    assert b"Not shown" in page.content and b"1 MiB" in page.content
    assert "\u202e".encode() not in page.content


def test_passwordless_loopback_requires_owner_launch_token(broker, settings):
    settings.BROKER_LOCAL_AUTH_MODE = "none"
    settings.BROKER_LOCAL_RUNTIME = "1"
    settings.BROKER_LOCAL_OWNER_ID = broker.owner.id
    settings.BROKER_LOCAL_ORIGIN = "http://127.0.0.1:8765"
    client = Client()
    client.get("/", HTTP_HOST="127.0.0.1:8765", HTTP_ACCEPT="text/html")
    assert "_auth_user_id" not in client.session


def test_duplicate_report_committed_during_storage_replays_receipt(broker):
    import base64
    from compute_relay.control.models import LedgerEntry, WorkerReport
    budgeted(broker)
    attempt, lease = dispatched(broker, prepared_job(broker, outputs=["result.txt"]))
    native, lease = submit_receipt(broker, attempt, lease)
    data = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "observed", "native_identity": native,
        "artifacts": {"result.txt": base64.b64encode(b"done").decode()},
        "observation": {"state": "completed", "terminal": True,
            "accounting": {"final": True, "cpu_core_seconds": 2, "gpu_device_seconds": 0}}}
    responses = []
    original = services.store_blob
    def commit_other_report(*args, **kwargs):
        with patch.object(services, "store_blob", original):
            responses.append(services.report(broker.worker, attempt.id, data))
        return original(*args, **kwargs)
    with patch.object(services, "store_blob", side_effect=commit_other_report):
        result = services.report(broker.worker, attempt.id, data)
    assert result == responses[0]
    assert WorkerReport.objects.filter(lease_id=lease["id"]).count() == 1
    assert LedgerEntry.objects.filter(attempt=attempt, action="settle").count() == 1


@pytest.mark.parametrize("partitions", [[None, "bad", 7], None, "bad"])
def test_old_malformed_availability_remains_readable(broker, partitions):
    from compute_relay.control.availability import public_availability
    broker.connection.availability_snapshot = {"partitions": partitions, "observed_at": 17}
    assert public_availability(broker.connection)["partitions"] == []
