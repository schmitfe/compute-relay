"""Bounded end-to-end transfer and live-output acceptance without real compute."""

import asyncio
from unittest.mock import AsyncMock
import base64
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from django.db import transaction
from django.test import Client
from django.utils import timezone
import pytest

from compute_relay.backends import SlurmBackend, prepare
from compute_relay.backends.preparation import sha256, canonical_json_bytes, JOB_PROTOCOL
from compute_relay.control import services
from compute_relay.control.models import Artifact, Attempt, Blob, LedgerEntry, UploadGrant, UploadReservation
from compute_relay.control.storage import _writer_lock, begin_transfer, blob_path, transfer_chunk, verify_blob
from compute_relay.domain.lifecycle import PreparedWork
from compute_relay.remote import helper
from compute_relay.transfer import CHUNK_BYTES, FileReference, MAX_FILE_BYTES
from compute_relay.worker.journal import Journal
from compute_relay.worker.runner import Worker
from compute_relay.worker.transfers import collect_outputs, stage_inputs
from compute_relay.worker.client import ControlError, _http_error
from compute_relay.workspace import WorkspaceClient
from test_support.unit_test_control import broker, budgeted, dispatched, prepared_job, submit_receipt
from test_support.unit_test_workspace import workspace
from test_support.unit_test_backends import sample, ATTEMPT, REVISION


class HTTP:
    def __init__(self, token, *, record_bytes=True):
        import threading
        self.client = Client(HTTP_AUTHORIZATION="Bearer " + token)
        self.calls = []
        self.fail_at = None
        self.record_bytes = record_bytes
        self._lock = threading.RLock()

    def request(self, method, path, body=None, **kwargs):
        # Django's in-process client and shared-cache in-memory SQLite are not
        # concurrent HTTP servers. Serialize fixture calls, including renewals.
        with self._lock:
            return self._request(method, path, body, **kwargs)

    def _request(self, method, path, body=None, **kwargs):
        recorded = body
        if not self.record_bytes and body and "content_base64" in body:
            recorded = {key: value for key, value in body.items() if key != "content_base64"}
            recorded["encoded_chunk_bytes"] = len(body["content_base64"])
        self.calls.append((method, path, recorded))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise ControlError("CONTROL_UNAVAILABLE")
        response = self.client.generic(method, path, data=json.dumps(body or {}), content_type="application/json")
        if response.status_code >= 400:
            raise _http_error(response.status_code, response.content)
        return response.json()

    def get_bytes(self, path, limit):
        self.calls.append(("GET_BYTES", path, limit))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise ControlError("CONTROL_UNAVAILABLE")
        response = self.client.get(path)
        assert response.status_code == 200
        assert len(response.content) <= limit
        return response.content, response["Content-Type"]


class LocalTransport:
    def __init__(self):
        self.calls = []

    async def call_helper(self, source, request):
        assert len(json.dumps(request)) < 2 * CHUNK_BYTES + 65536
        self.calls.append(request)
        return helper.execute(request["config"], request)


class AsyncHTTP:
    def __init__(self, sync):
        self.sync = sync

    async def request(self, *args, **kwargs):
        return self.sync.request(*args, **kwargs)




def post(client, path, data):
    return client.post(path, data=json.dumps(data), content_type="application/json")


def test_resumable_upload_replays_chunks_checks_hash_and_reserves_once(broker):
    data = b"x" * CHUNK_BYTES + b"remaining"
    digest = sha256(data)
    api = HTTP(broker.token)
    path = f"/api/v1/projects/{broker.project.id}/transfers"
    first = api.request("POST", path, {"sha256": digest, "size_bytes": len(data)})
    chunk = {"offset": 0, "content_base64": base64.b64encode(data[:CHUNK_BYTES]).decode()}
    endpoint = path + "/" + first["transfer_id"]
    assert api.request("POST", endpoint, chunk)["offset"] == CHUNK_BYTES
    assert api.request("POST", endpoint, chunk)["offset"] == CHUNK_BYTES
    resumed = api.request("POST", path, {"sha256": digest, "size_bytes": len(data)})
    assert resumed["transfer_id"] == first["transfer_id"] and resumed["offset"] == CHUNK_BYTES
    broker.project.refresh_from_db()
    assert broker.project.storage_reserved == len(data)
    assert UploadReservation.objects.filter(state="reserved").count() == 1
    final = api.request("POST", endpoint, {"offset": CHUNK_BYTES,
        "content_base64": base64.b64encode(data[CHUNK_BYTES:]).decode(), "finish": True})
    assert final["complete"] and final["sha256"] == digest
    blob = Blob.objects.get(project=broker.project, sha256=digest)
    verify_blob(blob)
    assert UploadGrant.objects.filter(blob=blob).exists()
    broker.project.refresh_from_db()
    assert broker.project.storage_reserved == 0 and broker.project.storage_used == len(data)
    assert api.request("POST", endpoint, {"offset": CHUNK_BYTES, "finish": True}) == final


def test_transfer_scope_hash_conflicts_quotas_and_large_sizes(broker, settings):
    api = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    path = f"/api/v1/projects/{broker.project.id}/transfers"
    assert post(api, path, {"sha256": "a" * 64, "size_bytes": MAX_FILE_BYTES + 1}).status_code == 413
    assert post(api, path, {"sha256": "a" * 64, "size_bytes": True}).status_code == 413
    assert post(api, f"/api/v1/projects/{uuid4()}/transfers", {"sha256": "a" * 64, "size_bytes": 2}).status_code == 404
    settings.PROJECT_STORAGE_LIMIT = 2
    assert post(api, path, {"sha256": "a" * 64, "size_bytes": 3}).status_code == 409
    settings.PROJECT_STORAGE_LIMIT = 64 * 1024**3
    # Reserving a multi-GiB object does not read or allocate its contents.
    first = post(api, path, {"sha256": "a" * 64, "size_bytes": 3 * 1024**3}).json()
    broker.project.refresh_from_db()
    assert broker.project.storage_reserved == 3 * 1024**3
    reservation = UploadReservation.objects.get(id=first["transfer_id"])
    assert (settings.ARTIFACT_ROOT / str(broker.project.id) / ("upload-" + str(reservation.id))).stat().st_size == 0
    small = post(api, path, {"sha256": sha256(b"ok"), "size_bytes": 2}).json()
    endpoint = path + "/" + small["transfer_id"]
    assert post(api, endpoint, {"offset": 1, "content_base64": "YQ=="}).status_code == 409
    assert post(api, endpoint, {"offset": 0, "content_base64": "bm8=", "finish": True}).status_code == 409
    assert not Blob.objects.filter(sha256=sha256(b"ok")).exists()
    assert post(api, endpoint, {"offset": 0, "content_base64": "b2s="}).status_code == 409


def test_hash_only_upload_does_not_grant_artifact_input_authority(broker):
    from compute_relay.control.storage import store_blob
    blob = store_blob(broker.project, b"secret output")
    response = begin_transfer(broker.project, blob.sha256, blob.size_bytes, "upload:" + str(broker.credential.id), uploaded=True)
    assert response["complete"] is False and not UploadGrant.objects.filter(blob=blob).exists()
    # Completion without actually receiving the independently supplied bytes fails.
    with pytest.raises(Exception):
        transfer_chunk(broker.project, response["transfer_id"], "upload:" + str(broker.credential.id), 0, b"", finish=True, uploaded=True)
    assert not UploadGrant.objects.filter(blob=blob).exists()


def test_workspace_large_upload_and_interrupted_download_resume(broker, tmp_path):
    from compute_relay.control.auth import token_hash
    broker.token = "cbp_synthetic_large_transfer_project_token"
    broker.credential.token_hash = token_hash(broker.token)
    broker.credential.save(update_fields=["token_hash"])
    root = workspace(tmp_path / "workspace", token=broker.token)
    data = bytes(range(256)) * (12 * 4096) + b"last byte"
    (root / "checkpoint.dat").write_bytes(data)
    api = HTTP(broker.token)
    client = WorkspaceClient(root, client=api)
    staged = client.stage(["checkpoint.dat"])
    assert staged["size_bytes"] == len(data) and staged["uploaded_objects"] == 1
    assert client.stage(["checkpoint.dat"])["reused_objects"] == 1
    assert max(len(json.dumps(body)) for _, _, body in api.calls if body) < 2 * CHUNK_BYTES
    job = prepared_job(broker, outputs=["checkpoint.dat"])
    budgeted(broker)
    attempt, _ = dispatched(broker, job)
    blob = Blob.objects.get(project=broker.project, sha256=sha256(data))
    Artifact.objects.create(attempt=attempt, path="checkpoint.dat", blob=blob)
    api.calls.clear()
    api.fail_at = 4  # inventory, chunk zero, chunk one, disconnect
    with pytest.raises(ControlError):
        client.fetch(str(attempt.id), "download")
    assert not (root / "download/.compute-relay-receipt.json").exists()
    api.calls.clear();api.fail_at = None
    result = client.fetch(str(attempt.id), "download", resume=True)
    assert (root / "download/checkpoint.dat").read_bytes() == data
    assert result["integrity"] == "verified" and result["scientific_validation"] == "not_checked"
    reads = [path for method, path, _ in api.calls if method == "GET_BYTES"]
    assert reads[0].endswith(f"offset={2 * CHUNK_BYTES}")
    assert client.fetch(str(attempt.id), "download", resume=True)["integrity"] == "verified"


def remote_package(sample, tmp_path, monkeypatch, *, large_input=False):
    request, environment, connection, content = deepcopy(sample)
    data = b"input-data\0" * (CHUNK_BYTES // 5)
    if large_input:
        request["files"].append({"path": "data/input.bin", "sha256": sha256(data), "size_bytes": len(data), "executable": False})
    prepared = prepare(request, environment, connection, REVISION)
    files = {"main.py": content, **{path: text.encode() for path, text in prepared["generated_files"].items()}}
    if large_input:
        files["data/input.bin"] = FileReference(sha256(data), len(data))
    config = {**connection, "remote_root": str(tmp_path / "remote")}
    transport = LocalTransport()
    backend = SlurmBackend(config, transport=transport)
    calls = []
    def scheduler(config, command, arguments, **kwargs):
        calls.append(command)
        return {"returncode": 0, "stdout": "42\n", "stderr": ""}
    monkeypatch.setattr(helper, "scheduler", scheduler)
    return backend, prepared, files, data, calls


def test_remote_large_input_resume_before_single_submission(sample, tmp_path, monkeypatch):
    backend, prepared, files, data, calls = remote_package(sample, tmp_path, monkeypatch, large_input=True)
    manifest = prepared["manifest"]
    first = asyncio.run(backend.stage_input(ATTEMPT, manifest, "data/input.bin", offset=0,
        content_base64=base64.b64encode(data[:CHUNK_BYTES]).decode(), finish=False))
    assert first["offset"] == CHUNK_BYTES and calls == []
    class Input:
        def request(self, method, path, body):
            offset = body["offset"]
            assert offset >= CHUNK_BYTES
            return {"offset": offset, "content_base64": base64.b64encode(data[offset:offset + CHUNK_BYTES]).decode()}
    lease = {"attempt_id": ATTEMPT, "id": str(uuid4()), "generation": 1}
    backend = SlurmBackend(backend.connection, transport=LocalTransport())
    asyncio.run(stage_inputs(AsyncHTTP(Input()), backend, lease, manifest, files))
    submitted = asyncio.run(backend.submit(ATTEMPT, manifest, files))
    assert submitted["outcome"] == "submitted" and calls == ["sbatch"]
    assert (Path(submitted["native_identity"]["work_dir"]) / "data/input.bin").read_bytes() == data


def test_same_content_inputs_keep_independent_partial_offsets(sample, tmp_path, monkeypatch):
    from copy import deepcopy
    sample = deepcopy(sample)
    data = b"streamed input" * 100000
    for path in ("data/one.bin", "data/two.bin"):
        sample[0]["files"].append({"path": path, "sha256": sha256(data), "size_bytes": len(data), "executable": False})
    backend, prepared, _, _, _ = remote_package(sample, tmp_path, monkeypatch)
    manifest = prepared["manifest"]
    first = asyncio.run(backend.stage_input(ATTEMPT, manifest, "data/one.bin", offset=0,
        content_base64=base64.b64encode(data[:10]).decode()))
    assert first["offset"] == 10
    assert asyncio.run(backend.stage_input(ATTEMPT, manifest, "data/two.bin"))["offset"] == 0
    asyncio.run(backend.stage_input(ATTEMPT, manifest, "data/two.bin", offset=0,
        content_base64=base64.b64encode(data[:20]).decode()))
    assert asyncio.run(backend.stage_input(ATTEMPT, manifest, "data/one.bin"))["offset"] == 10


def test_legacy_collection_stops_after_aggregate_cap(sample, tmp_path, monkeypatch):
    sample = deepcopy(sample)
    sample[0]["outputs"] = ["first.bin", "second.bin"]
    backend, prepared, files, _, _ = remote_package(sample, tmp_path, monkeypatch)
    native = asyncio.run(backend.submit(ATTEMPT, prepared["manifest"], files))["native_identity"]
    directory = Path(native["work_dir"])
    (directory / "first.bin").write_bytes(b"x" * (10 * 1024 * 1024))
    (directory / "second.bin").write_bytes(b"more")
    result = asyncio.run(backend.collect(native, sample[0]["outputs"], manifest=prepared["manifest"]))
    assert set(result["artifacts"]) == {"first.bin"}


def test_remote_output_manifest_and_ranges_reject_changes_and_symlinks(sample, tmp_path, monkeypatch):
    backend, prepared, files, _, _ = remote_package(sample, tmp_path, monkeypatch)
    native = asyncio.run(backend.submit(ATTEMPT, prepared["manifest"], files))["native_identity"]
    directory = Path(native["work_dir"])
    (directory / "results").mkdir()
    output = directory / "results/metrics.json"
    output.write_bytes(b"x" * (11 * CHUNK_BYTES))
    inventory = asyncio.run(backend.output_manifest(native, manifest=prepared["manifest"]))
    from compute_relay.worker.client import strict_json
    # Persistent SSH frames reject integers outside the exact wire range.
    assert strict_json(json.dumps(inventory).encode()) == inventory
    entry = inventory["files"][0]
    assert entry["size_bytes"] > 10 * CHUNK_BYTES
    chunk = asyncio.run(backend.output_chunk(native, manifest=prepared["manifest"], path=entry["path"], offset=10 * CHUNK_BYTES, version=entry["version"]))
    assert len(base64.b64decode(chunk["content_base64"])) == CHUNK_BYTES
    output.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(backend.output_chunk(native, manifest=prepared["manifest"], path=entry["path"], offset=0, version=entry["version"]))
    output.unlink();output.symlink_to(tmp_path / "outside")
    with pytest.raises(OSError):
        asyncio.run(backend.output_manifest(native, manifest=prepared["manifest"]))


def test_live_logs_arrive_before_completion_report_gaps_and_reset(sample, tmp_path, monkeypatch):
    backend, prepared, files, _, _ = remote_package(sample, tmp_path, monkeypatch)
    native = asyncio.run(backend.submit(ATTEMPT, prepared["manifest"], files))["native_identity"]
    log = Path(native["work_dir"]) / "stdout.log"
    log.write_bytes(b"realization 1 done\n")
    first = asyncio.run(backend.read_logs(native, manifest=prepared["manifest"], cursors={}))["stdout"]
    assert base64.b64decode(first["content_base64"]) == b"realization 1 done\n"
    with log.open("ab") as stream:
        stream.write(b"realization 2 done\n")
    cursors = {"stdout": {"cursor": first["next_cursor"], "file_id": first["file_id"]}}
    second = asyncio.run(backend.read_logs(native, manifest=prepared["manifest"], cursors=cursors))["stdout"]
    assert base64.b64decode(second["content_base64"]) == b"realization 2 done\n"
    log.write_bytes(b"x" * 100000)
    large = asyncio.run(backend.read_logs(native, manifest=prepared["manifest"], cursors=cursors))["stdout"]
    assert large["truncated"] and len(base64.b64decode(large["content_base64"])) == 65536
    log.write_bytes(b"restart\n")
    reset = asyncio.run(backend.read_logs(native, manifest=prepared["manifest"], cursors=cursors))["stdout"]
    assert reset["reset"] and base64.b64decode(reset["content_base64"]) == b"restart\n"
    log.unlink();os.mkfifo(log)
    with pytest.raises(ValueError, match="regular files"):
        asyncio.run(backend.read_logs(native, manifest=prepared["manifest"], cursors={}))


def test_live_log_report_cursor_preserves_raw_utf8_and_no_accounting_settlement(broker):
    budgeted(broker)
    job = prepared_job(broker)
    attempt, lease = dispatched(broker, job)
    native, lease = submit_receipt(broker, attempt, lease)
    content = b"progress \xe2\x82\xac\xff\n"
    report = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "observed", "native_identity": native,
        "observation": {"state": "running", "terminal": False, "accounting": {"final": False}},
        "log_chunks": {"stdout": {"cursor": 0, "next_cursor": len(content), "size_bytes": len(content),
            "file_id": "1:2", "reset": True, "truncated": False, "content_base64": base64.b64encode(content).decode()}}}
    services.report(broker.worker, attempt.id, report)
    attempt.refresh_from_db()
    assert attempt.state == "running" and attempt.accounting_state == "reserved"
    api = HTTP(broker.token)
    result = api.request("GET", f"/api/v1/attempts/{attempt.id}/logs?stream=stdout&cursor=0&limit=65536")
    assert base64.b64decode(result["content_base64"]) == content and not result["finished"]
    assert result["updated_at"] and result["eof"]
    empty = api.request("GET", f"/api/v1/attempts/{attempt.id}/logs?stream=stdout&cursor={len(content)}")
    assert empty["content_base64"] == "" and empty["next_cursor"] == len(content)


def test_collection_failure_retries_settled_attempt_without_new_compute_or_charge(broker):
    budgeted(broker)
    job = prepared_job(broker, outputs=["results.bin"])
    attempt, lease = dispatched(broker, job)
    native, lease = submit_receipt(broker, attempt, lease)
    report = {"lease_id": lease["id"], "generation": lease["generation"], "outcome": "observed", "native_identity": native,
        "observation": {"state": "completed", "terminal": True, "accounting": {"final": True, "cpu_core_seconds": 2, "gpu_device_seconds": 0}},
        "collection_state": "failed", "error_code": "OUTPUT_COLLECTION_FAILED"}
    services.report(broker.worker, attempt.id, report)
    attempt.refresh_from_db()
    assert attempt.accounting_state == "settled" and attempt.collection_pending and attempt.collection_state == "retry_pending"
    for index in range(2):
        Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now())
        retry = services.lease_work(broker.worker)["lease"]
        assert retry["collection_only"] and retry["operation"] == "observe" and retry["observation"] == report["observation"]
        services.report(broker.worker, attempt.id, {**report, "lease_id": retry["id"], "generation": retry["generation"]})
    attempt.refresh_from_db()
    assert not attempt.collection_pending and attempt.collection_state == "failed"
    assert LedgerEntry.objects.filter(attempt=attempt, action="settle").count() == 1
    result = HTTP(broker.token).request("POST", f"/api/v1/attempts/{attempt.id}/collect", {})
    assert result["collection_state"] == "pending"
    retry = services.lease_work(broker.worker)["lease"]
    assert retry["collection_only"]
    services.report(broker.worker, attempt.id, {**report, "lease_id": retry["id"], "generation": retry["generation"],
        "collection_state": "complete", "error_code": ""})
    attempt.refresh_from_db();broker.grant.refresh_from_db()
    assert attempt.collection_state == "complete" and attempt.error_code == "" and not attempt.collection_pending
    assert broker.grant.spent_cpu == 2 and broker.grant.reserved_cpu == 0
    assert Attempt.objects.count() == 1 and LedgerEntry.objects.filter(attempt=attempt, action="settle").count() == 1


@pytest.mark.django_db(transaction=True)
def test_collection_retries_busy_publication_without_restarting_remote_reads(broker, monkeypatch):
    monkeypatch.setenv("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
    budgeted(broker)
    job = prepared_job(broker, outputs=["result.json"])
    attempt, first = dispatched(broker, job)
    native, lease = submit_receipt(broker, attempt, first)
    data = b"correct result"
    backend = AsyncMock()
    backend.output_manifest.return_value = {"files": [{"path": "result.json", "sha256": sha256(data),
        "size_bytes": len(data), "version": "unchanged"}]}
    backend.output_chunk.return_value = {"offset": 0, "content_base64": base64.b64encode(data).decode()}
    api = HTTP("synthetic-worker-token")
    request = api._request
    blocked = set()

    def contended(method, path, body=None, **kwargs):
        step = body.get("offset", "descriptor")
        if step not in blocked:
            blocked.add(step)
            with _writer_lock(broker.project.id):
                return request(method, path, body, **kwargs)
        return request(method, path, body, **kwargs)

    api._request = contended
    result = asyncio.run(collect_outputs(AsyncHTTP(api), backend, lease, native, job.manifest, asyncio.Lock()))
    assert result["collection_state"] == "complete"
    assert len(api.calls) == 4 and api.calls[0] == api.calls[1] and api.calls[2] == api.calls[3]
    backend.output_manifest.assert_awaited_once()
    backend.output_chunk.assert_awaited_once_with(native, manifest=job.manifest, path="result.json", offset=0, version="unchanged")
    artifact = Artifact.objects.get(attempt=attempt, path="result.json")
    assert artifact.blob.sha256 == sha256(data) and artifact.blob.size_bytes == len(data)
    assert Artifact.objects.filter(attempt=attempt).count() == 1
    broker.project.refresh_from_db()
    assert broker.project.storage_reserved == 0 and not UploadReservation.objects.filter(state="reserved").exists()


@pytest.mark.django_db(transaction=True)
def test_worker_streams_live_output_and_large_artifacts_through_broker(broker, tmp_path, monkeypatch):
    # The helper runs only fixed reviewed code. The fixture scheduler never runs the uploaded script.
    monkeypatch.setenv("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
    budgeted(broker)
    job = prepared_job(broker, outputs=["results/metrics.bin"])
    attempt, first = dispatched(broker, job)
    config = {**broker.connection.configuration, "revision_sha256": broker.connection.revision_sha256,
        "remote_root": str(tmp_path / "remote")}
    monkeypatch.setattr(helper, "scheduler", lambda *a, **kw: {"returncode": 0, "stdout": "42\n", "stderr": ""})
    backend = SlurmBackend(config, transport=LocalTransport())
    work = PreparedWork.decode(first)
    native = asyncio.run(backend.submit(str(attempt.id), job.manifest, work.files))["native_identity"]
    services.report(broker.worker, attempt.id, {"lease_id": first["id"], "generation": first["generation"],
        "outcome": "submitted", "native_identity": native})
    directory = Path(native["work_dir"])
    log = directory / "stdout.log"
    log.write_bytes(b"realization 1/2 done\n")
    worker_api = HTTP("synthetic-worker-token")
    backend = SlurmBackend(config, transport=LocalTransport())
    worker = Worker(AsyncHTTP(worker_api), {str(broker.connection.id): config}, Journal(tmp_path / "journal"),
        backend_factory=lambda _: backend, extensions=[], owner_id=uuid4())
    observation = {"state": "running", "terminal": False, "accounting": {"final": False}}
    def invoke():
        Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now())
        lease = services.lease_work(broker.worker)["lease"]
        async def observe(items):
            return {item["native_identity"]["attempt_id"]: deepcopy(observation) for item in items}
        backend.observe = observe
        asyncio.run(worker.execute(lease))
    invoke()
    attempt.refresh_from_db()
    assert attempt.state == "running" and attempt.stdout == "realization 1/2 done\n"
    assert not Artifact.objects.filter(attempt=attempt).exists()
    (directory / "results").mkdir()
    data = b"record\0" * (2 * CHUNK_BYTES)  # 14 MiB, beyond the former aggregate limit.
    (directory / "results/metrics.bin").write_bytes(data)
    log.write_bytes(b"realization 1/2 done\nrealization 2/2 done\n")
    observation.update(state="completed", terminal=True, accounting={"final": True, "cpu_core_seconds": 2, "gpu_device_seconds": 0})
    invoke()
    attempt.refresh_from_db()
    assert attempt.state == "succeeded" and attempt.accounting_state == "settled"
    assert attempt.collection_state == "complete" and "realization 2/2" in attempt.stdout
    artifact = Artifact.objects.get(attempt=attempt, path="results/metrics.bin")
    assert artifact.blob.size_bytes == len(data) and artifact.blob.sha256 == sha256(data)
    verify_blob(artifact.blob)
    assert max(len(json.dumps(body)) for _, _, body in worker_api.calls if body) < 2 * CHUNK_BYTES
    assert not any(body.get("artifacts") for _, path, body in worker_api.calls if path.endswith("/report"))


def test_expired_hash_only_upload_cannot_acquire_grant_during_recovery(broker):
    from datetime import timedelta
    from compute_relay.control.errors import BrokerError
    from compute_relay.control.storage import reconcile_uploads, store_blob
    blob = store_blob(broker.project, b"artifact-only")
    binding = "upload:" + str(broker.credential.id)
    first = begin_transfer(broker.project, blob.sha256, blob.size_bytes, binding, uploaded=True)
    UploadReservation.objects.filter(id=first["transfer_id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert reconcile_uploads()["recovered"] == 1
    assert UploadReservation.objects.get(id=first["transfer_id"]).state == "aborted"
    with pytest.raises(BrokerError):
        transfer_chunk(broker.project, first["transfer_id"], binding, 0, b"", finish=True, uploaded=True)
    assert not UploadGrant.objects.filter(blob=blob).exists()


@pytest.mark.skipif(os.environ.get("COMPUTE_RELAY_LARGE_TRANSFER_TEST") != "1", reason="opt-in multi-GiB disk acceptance")
@pytest.mark.django_db(transaction=True)
def test_multigib_roundtrip_is_bounded_across_workspace_broker_and_worker(broker, tmp_path, monkeypatch):
    import shutil
    import time
    import tracemalloc
    from compute_relay.control.auth import token_hash
    from compute_relay.control.models import Job
    from test_control import execution

    started = time.monotonic()
    size = 2 * 1024**3 + 17
    broker.token = "cbp_synthetic_multigib_acceptance_token"
    broker.credential.token_hash = token_hash(broker.token)
    broker.credential.save(update_fields=["token_hash"])
    root = workspace(tmp_path / "workspace", token=broker.token)
    with (root / "input.bin").open("wb") as stream:
        stream.truncate(size)
    public = HTTP(broker.token, record_bytes=False)
    workspace_client = WorkspaceClient(root, client=public)
    tracemalloc.start()
    staged = workspace_client.stage(["input.bin"])
    assert staged["size_bytes"] == size
    bundle = json.loads((root / staged["receipt"]).read_bytes())
    budgeted(broker)
    template = prepared_job(broker, outputs=["output.bin"])
    created, _ = services.create_job(broker.credential, {**template.request,
        "files": [*template.request["files"], *bundle["files"]]})
    job = Job.objects.get(id=created["id"])
    services.prepare_job(job);job.refresh_from_db()
    request = execution(broker, job)
    lease = services.lease_work(broker.worker)["lease"]
    config = {**broker.connection.configuration, "revision_sha256": broker.connection.revision_sha256,
        "remote_root": str(tmp_path / "remote")}
    monkeypatch.setattr(helper, "scheduler", lambda *a, **kw: {"returncode": 0, "stdout": "42\n", "stderr": ""})
    class UnrecordedTransport:
        async def call_helper(self, source, request):
            return helper.execute(request["config"], request)
    submission_errors = []
    class DiagnosticBackend(SlurmBackend):
        async def submit(self, *args, **kwargs):
            try:
                return await super().submit(*args, **kwargs)
            except Exception as error:
                submission_errors.append(type(error).__name__ + ": " + str(error))
                raise
    backend = DiagnosticBackend(config, transport=UnrecordedTransport())
    private = HTTP("synthetic-worker-token", record_bytes=False)
    worker = Worker(AsyncHTTP(private), {str(broker.connection.id): config}, Journal(tmp_path / "journal"),
        backend_factory=lambda _: backend, extensions=[], owner_id=str(uuid4()))
    asyncio.run(worker.execute(lease))
    attempt = request.attempt
    attempt.refresh_from_db()
    assert attempt.state == "queued", submission_errors
    directory = Path(attempt.native_identity["work_dir"])
    shutil.copyfile(directory / "input.bin", directory / "output.bin")
    with (directory / "output.bin").open("r+b") as stream:
        stream.write(b"different output\n")
    backend.observe = AsyncMock(side_effect=lambda items: {item["native_identity"]["attempt_id"]:
        {"state": "completed", "terminal": True,
            "accounting": {"final": True, "cpu_core_seconds": 2, "gpu_device_seconds": 0}} for item in items})
    Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now())
    asyncio.run(worker.execute(services.lease_work(broker.worker)["lease"]))
    attempt.refresh_from_db()
    assert attempt.collection_state == "complete"
    receipt = workspace_client.fetch(str(attempt.id), "download")
    assert receipt["size_bytes"] == size and receipt["integrity"] == "verified"
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 128 * 1024**2
    metrics = {"file_bytes": size, "directions": ["workspace_to_broker", "broker_to_cluster_helper",
        "cluster_helper_to_broker", "broker_to_workspace"], "peak_traced_python_bytes": peak,
        "elapsed_seconds": round(time.monotonic() - started, 3), "integrity": "verified",
        "real_ssh_or_slurm": False}
    report = os.environ.get("COMPUTE_RELAY_TRANSFER_REPORT")
    if report:
        Path(report).write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics))


def test_follow_cli_preserves_utf8_split_between_snapshots(monkeypatch, capsys):
    from types import SimpleNamespace
    from compute_relay.workspace import follow_logs
    packets = iter([
        {"file_id": "1:2", "content_base64": base64.b64encode(b"progress \xe2").decode(), "next_cursor": 10,
         "eof": True, "finished": False},
        {"file_id": "1:2", "content_base64": base64.b64encode(b"\x82\xac\n").decode(), "next_cursor": 13,
         "eof": True, "finished": True}])
    requests = []
    class Facade:
        def get_job_logs(self, attempt, stream, cursor, limit):
            requests.append(cursor)
            return next(packets)
    monkeypatch.setattr("time.sleep", lambda _: None)
    helper = SimpleNamespace(_facade=lambda: Facade())
    follow_logs(helper, SimpleNamespace(attempt_id=str(uuid4()), stream="stdout", cursor=0, follow=True))
    assert capsys.readouterr().out == "progress €\n" and requests == [0, 10]


def test_stale_worker_cannot_transfer_outputs_or_read_inputs(broker):
    budgeted(broker)
    job = prepared_job(broker, outputs=["results.bin"])
    attempt, first = dispatched(broker, job)
    native, lease = submit_receipt(broker, attempt, first)
    api = HTTP("synthetic-worker-token")
    with pytest.raises(ControlError) as error:
        api.request("POST", f"/internal/v1/attempts/{attempt.id}/output-chunk", {
            "lease_id": first["id"], "generation": first["generation"], "path": "results.bin",
            "sha256": "a" * 64, "size_bytes": 12})
    assert error.value.status == 409 and not UploadReservation.objects.filter(binding__startswith="output:").exists()
    with pytest.raises(ControlError) as error:
        api.request("POST", f"/internal/v1/attempts/{attempt.id}/input-chunk", {
            "lease_id": lease["id"], "generation": lease["generation"], "path": "main.py", "offset": 0})
    assert error.value.status == 403
    with pytest.raises(ControlError) as error:
        api.request("POST", f"/internal/v1/attempts/{attempt.id}/output-chunk", {
            "lease_id": lease["id"], "generation": lease["generation"], "path": "undeclared.bin",
            "sha256": "a" * 64, "size_bytes": 12})
    assert error.value.status == 403


def test_storage_failures_stop_after_three_legacy_collection_reports(broker, settings):
    budgeted(broker)
    job = prepared_job(broker, outputs=["results.bin"])
    attempt, first = dispatched(broker, job)
    native, lease = submit_receipt(broker, attempt, first)
    settings.PROJECT_STORAGE_LIMIT = 1
    for number in range(3):
        services.report(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
            "outcome": "observed", "native_identity": native, "collection_state": "complete",
            "observation": {"state": "completed", "terminal": True,
                "accounting": {"final": True, "cpu_core_seconds": 2, "gpu_device_seconds": 0}},
            "artifacts": {"results.bin": base64.b64encode(b"too large for this quota").decode()}})
        attempt.refresh_from_db()
        assert attempt.collection_failures == number + 1
        if number < 2:
            Attempt.objects.filter(id=attempt.id).update(next_poll_at=timezone.now())
            lease = services.lease_work(broker.worker)["lease"]
    assert attempt.collection_state == "failed" and not attempt.collection_pending
    assert LedgerEntry.objects.filter(attempt=attempt, action="settle").count() == 1
