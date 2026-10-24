"""Explicit workspace transfers through project-owned APIs, never source execution."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event
from uuid import uuid4

from django.test import Client
import pytest

from compute_relay.control.auth import token_hash
from compute_relay.control.models import Artifact, Attempt, ExecutionRequest, Job, Project
from compute_relay.control.storage import store_blob
from compute_relay import workspace as workspace_module
from compute_relay.workspace import WorkspaceClient, MAX_SOURCE_BYTES, main
from test_control import broker, budgeted, execution, prepared_job  # noqa: F401
from test_project_context import context_live_server  # noqa: F401


TOKEN = "cbp_" + "synthetic_workspace_only_" * 2
PROJECT = str(uuid4())


def workspace(path, url="http://127.0.0.1:8768", token=TOKEN):
    path.mkdir(exist_ok=True)
    private = path / ".compute-relay"
    private.mkdir(mode=0o700)
    (private / "project.key").write_text(token)
    (private / "project.key").chmod(0o600)
    (private / "connection.json").write_text(json.dumps({"schema_version": "1.0", "broker_url": url,
        "workspace_root": str(path)}))
    (private / "connection.json").chmod(0o600)
    return path


class TransferClient:
    def __init__(self):
        self.calls = []
        self.content = {}
        self.present = None
        self.inventory = []
        self.downloads = {}

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body, kwargs))
        if path == "/api/v1/context":
            return {"project": {"id": PROJECT}}
        if path.endswith("/objects/lookup"):
            return self.present if self.present is not None else {"present": [entry for entry in body["objects"]
                if entry["sha256"] in self.content]}
        if path.endswith("/objects"):
            data = base64.b64decode(body["content_base64"], validate=True)
            digest = hashlib.sha256(data).hexdigest()
            self.content[digest] = data
            return {"path": body["path"], "sha256": digest, "size_bytes": len(data), "executable": False}
        if path.endswith("/artifacts"):
            return {"artifacts": self.inventory}
        raise AssertionError("Unexpected API call")

    def get_bytes(self, path, maximum):
        self.calls.append(("BYTES", path, maximum))
        return self.downloads[path], "application/octet-stream"


def artifact(client, path="results/data.csv", data=b"x,y\n1,2\n", **changes):
    identity = str(uuid4())
    entry = {"id": identity, "path": path, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data), **changes}
    client.inventory.append(entry)
    client.downloads[f"/api/v1/artifacts/{identity}/content"] = data
    return entry


def test_stage_explicit_files_deduplicates_content_and_writes_private_stable_receipt(tmp_path):
    root = workspace(tmp_path / "science")
    (root / "main.py").write_bytes(b"raise RuntimeError('must never execute in transfer')\n")
    (root / "copy.py").write_bytes((root / "main.py").read_bytes())
    (root / "main.py").chmod(0o700)
    client = TransferClient()
    helper = WorkspaceClient(root, client=client)
    first = helper.stage(["main.py", "copy.py"])
    second = helper.stage(["copy.py", "main.py"])
    assert first["bundle_id"] == second["bundle_id"]
    assert first["uploaded_objects"] == 1 and second["uploaded_objects"] == 0
    assert second["reused_objects"] == 1
    assert len([call for call in client.calls if call[1].endswith("/objects")]) == 1
    receipt = root / first["receipt"]
    assert receipt.stat().st_mode & 0o777 == 0o600
    document = json.loads(receipt.read_bytes())
    assert [entry["path"] for entry in document["files"]] == ["copy.py", "main.py"]
    assert [entry["executable"] for entry in document["files"]] == [False, True]
    assert "content_base64" not in receipt.read_text() and TOKEN not in receipt.read_text()
    assert "must never execute" not in json.dumps(first)


def test_atomic_editor_replacement_is_detected_after_the_open_inode_snapshot(tmp_path, monkeypatch):
    root = workspace(tmp_path / "science")
    source, replacement = root / "main.py", root / "editor-save.py"
    source.write_bytes(b"old source")
    replacement.write_bytes(b"new source")
    client = TransferClient();helper = WorkspaceClient(root, client=client)
    original_stat = source.stat()
    original_fstat, snapshots = os.fstat, []
    def snapshot_then_replace(descriptor):
        result = original_fstat(descriptor)
        if (result.st_dev, result.st_ino) == (original_stat.st_dev, original_stat.st_ino):
            snapshots.append(result)
            if len(snapshots) == 2:
                # Replace immediately after the last opened-inode snapshot;
                # both fstat results still describe the same unchanged bytes.
                replacement.replace(source)
        return result
    monkeypatch.setattr(workspace_module.os, "fstat", snapshot_then_replace)
    with pytest.raises(ValueError, match="changed during staging"):
        helper.stage(["main.py"])
    assert len(snapshots) == 2 and source.read_bytes() == b"new source"
    assert client.calls == []


def test_stage_stops_reading_when_the_aggregate_bound_is_reached(tmp_path, monkeypatch):
    root = workspace(tmp_path / "science")
    for name in ("first.py", "second.py", "unread.py"):
        (root / name).write_bytes(b"123")
    client = TransferClient();helper = WorkspaceClient(root, client=client)
    original_source, reads = helper._source, []
    def record_source(name):
        reads.append(name)
        return original_source(name)
    monkeypatch.setattr(helper, "_source", record_source)
    monkeypatch.setattr(workspace_module, "MAX_BUNDLE_BYTES", 4)
    with pytest.raises(ValueError, match="source bundle exceeds"):
        helper.stage(["first.py", "second.py", "unread.py"])
    assert reads == ["first.py", "second.py"] and client.calls == []


@pytest.mark.parametrize("operation", ["stage", "fetch"])
def test_interrupted_private_write_does_not_publish_partial_files_or_receipts(tmp_path, monkeypatch, operation):
    root = workspace(tmp_path / "science")
    (root / "main.py").write_bytes(b"explicit source")
    client = TransferClient();helper = WorkspaceClient(root, client=client)
    artifact(client)
    original_fdopen = os.fdopen
    class PartialWriter:
        def __init__(self, stream): self.stream = stream
        def write(self, content):
            self.stream.write(content[:max(1, len(content) // 2)])
            raise OSError("synthetic interrupted private write")
        def __getattr__(self, name): return getattr(self.stream, name)
    @contextmanager
    def interrupted(descriptor, mode, *args, **kwargs):
        with original_fdopen(descriptor, mode, *args, **kwargs) as stream:
            yield PartialWriter(stream) if mode == "wb" else stream
    with monkeypatch.context() as scoped:
        scoped.setattr(workspace_module.os, "fdopen", interrupted)
        with pytest.raises(OSError, match="interrupted private write"):
            if operation == "stage": helper.stage(["main.py"])
            else: helper.fetch(str(uuid4()), "results/interrupted")
    if operation == "stage":
        assert list((root / ".compute-relay/bundles").iterdir()) == []
        recovered = helper.stage(["main.py"])
        receipt = root / recovered["receipt"]
        assert hashlib.sha256(receipt.read_bytes()).hexdigest() == recovered["bundle_id"]
    else:
        assert not any(path.is_file() for path in (root / "results/interrupted").rglob("*"))
        recovered = helper.fetch(str(uuid4()), "results/recovered")
        assert (root / recovered["receipt"]).is_file()
    assert not list(root.rglob(".compute-relay-transfer-*"))


def test_concurrent_identical_staging_accepts_atomic_winner_and_durable_private_receipt(tmp_path, monkeypatch):
    root = workspace(tmp_path / "science")
    (root / "main.py").write_bytes(b"explicit source")
    client = TransferClient()
    helpers = [WorkspaceClient(root, client=client), WorkspaceClient(root, client=client)]
    publication, creation, winner_read = Barrier(2), Barrier(2), Event()
    original_link, original_mkdir, original_read = os.link, os.mkdir, workspace_module._read_published
    def simultaneous_create(path, *args, **kwargs):
        if str(path) == "bundles": creation.wait(timeout=5)
        return original_mkdir(path, *args, **kwargs)
    def simultaneous_publication(source, destination, *args, **kwargs):
        publication.wait(timeout=5)
        result = original_link(source, destination, *args, **kwargs)
        # Hold the successful publisher between link and temporary unlink so
        # the other staging operation must accept a complete two-link inode.
        assert winner_read.wait(5)
        return result
    def read_winner(directory, name, maximum):
        result = original_read(directory, name, maximum)
        winner_read.set()
        return result
    monkeypatch.setattr(workspace_module.os, "mkdir", simultaneous_create)
    monkeypatch.setattr(workspace_module.os, "link", simultaneous_publication)
    monkeypatch.setattr(workspace_module, "_read_published", read_winner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda helper: helper.stage(["main.py"]), helpers))
    assert results[0]["bundle_id"] == results[1]["bundle_id"]
    receipt = root / results[0]["receipt"]
    assert hashlib.sha256(receipt.read_bytes()).hexdigest() == results[0]["bundle_id"]
    assert receipt.stat().st_nlink == 1 and receipt.stat().st_mode & 0o777 == 0o600
    assert list(receipt.parent.iterdir()) == [receipt]


@pytest.mark.parametrize("selection", [[], ["missing.py"], ["../escape"], ["/etc/passwd"], ["a/../b"],
    [".compute-relay/project.key"], [".codex/config.toml"], [".claude/skills/x"], [".git/config"],
    [".ssh/id_ed25519"], [".aws/credentials"], ["_broker/entrypoint.sh"], ["same", "same"], [{}]])
def test_stage_rejects_unsafe_selection_before_network(tmp_path, selection):
    client = TransferClient()
    helper = WorkspaceClient(workspace(tmp_path / "science"), client=client)
    with pytest.raises((ValueError, FileNotFoundError)):
        helper.stage(selection)
    assert client.calls == []


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "hardlink", "directory", "fifo", "large"])
def test_source_links_special_files_and_oversize_are_never_read_or_uploaded(tmp_path, kind):
    root = workspace(tmp_path / "science")
    outside = tmp_path / "outside"
    outside.write_bytes(b"private-do-not-read")
    target = root / "input"
    name = "input"
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "parent_symlink":
        target.symlink_to(tmp_path, target_is_directory=True)
        name = "input/outside"
    elif kind == "hardlink":
        os.link(outside, target)
    elif kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(b"x" * (MAX_SOURCE_BYTES + 1))
    client = TransferClient()
    with pytest.raises((ValueError, OSError)):
        WorkspaceClient(root, client=client).stage([name])
    assert client.calls == []


@pytest.mark.parametrize("change", ["public_key", "public_config", "root_mismatch", "key_symlink", "metadata_symlink"])
def test_saved_connection_and_secret_must_be_private_and_bound_to_workspace(tmp_path, change):
    root = workspace(tmp_path / "science")
    key = root / ".compute-relay/project.key"
    config = root / ".compute-relay/connection.json"
    if change == "public_key":
        key.chmod(0o644)
    elif change == "public_config":
        config.chmod(0o644)
    elif change == "root_mismatch":
        config.write_text(config.read_text().replace(str(root), str(tmp_path / "other")))
    else:
        target = key if change == "key_symlink" else config
        copied = tmp_path / "copy"
        copied.write_bytes(target.read_bytes())
        copied.chmod(0o600)
        target.unlink()
        target.symlink_to(copied)
    with pytest.raises(ValueError):
        WorkspaceClient(root, client=TransferClient())


def test_lookup_response_cannot_assert_foreign_or_duplicate_content(tmp_path):
    root = workspace(tmp_path / "science")
    (root / "main.py").write_bytes(b"x")
    client = TransferClient()
    helper = WorkspaceClient(root, client=client)
    for response in ({"present": [{"sha256": "f" * 64, "size_bytes": 1}]},
        {"present": [{"sha256": hashlib.sha256(b"x").hexdigest(), "size_bytes": 1}] * 2},
        {"present": [], "secret": "private"}):
        client.present = response
        with pytest.raises(ValueError):
            helper.stage(["main.py"])
    assert not client.content


def test_fetch_verifies_bytes_and_records_integrity_separately_from_scientific_validation(tmp_path):
    root = workspace(tmp_path / "science")
    client = TransferClient()
    entry = artifact(client)
    helper = WorkspaceClient(root, client=client)
    result = helper.fetch(str(uuid4()), "results/run-1")
    assert (root / "results/run-1/results/data.csv").read_bytes() == b"x,y\n1,2\n"
    receipt = json.loads((root / result["receipt"]).read_bytes())
    assert receipt["files"] == [entry]
    assert receipt["integrity"] == "verified" and receipt["scientific_validation"] == "not_checked"
    assert (root / result["receipt"]).stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="new result directory"):
        helper.fetch(str(uuid4()), "results/run-1")


@pytest.mark.parametrize("kind", ["traversal", "absolute", "secret", "reserved_receipt", "duplicate", "prefix_collision", "oversize", "bool_size", "bad_digest"])
def test_fetch_inventory_rejected_before_creating_destination_or_downloading(tmp_path, kind):
    root = workspace(tmp_path / "science")
    client = TransferClient()
    entry = artifact(client)
    if kind in {"traversal", "absolute", "secret", "reserved_receipt"}:
        entry["path"] = {"traversal": "../escape", "absolute": "/tmp/escape", "secret": ".compute-relay/project.key",
            "reserved_receipt": ".compute-relay-receipt.json"}[kind]
    elif kind == "duplicate":
        artifact(client)
    elif kind == "prefix_collision":
        artifact(client, path="results")
    elif kind == "oversize":
        entry["size_bytes"] = 10 * 1024 * 1024 + 1
    elif kind == "bool_size":
        entry["size_bytes"] = True
    else:
        entry["sha256"] = "not-a-hash"
    with pytest.raises(ValueError):
        WorkspaceClient(root, client=client).fetch(str(uuid4()), "results/run")
    assert not (root / "results").exists()
    assert not any(call[0] == "BYTES" for call in client.calls)


def test_checksum_failure_never_writes_bad_content_or_success_receipt(tmp_path):
    root = workspace(tmp_path / "science")
    client = TransferClient()
    entry = artifact(client)
    client.downloads[f"/api/v1/artifacts/{entry['id']}/content"] = b"tampered"
    with pytest.raises(ValueError, match="checksum"):
        WorkspaceClient(root, client=client).fetch(str(uuid4()), "results/run")
    assert list((root / "results/run").iterdir()) == []


def test_download_cannot_follow_destination_parent_symlink(tmp_path):
    root = workspace(tmp_path / "science")
    (root / "results").symlink_to(tmp_path, target_is_directory=True)
    client = TransferClient()
    artifact(client)
    with pytest.raises(ValueError):
        WorkspaceClient(root, client=client).fetch(str(uuid4()), "results/run")
    assert not (tmp_path / "run").exists()


def test_cli_failure_does_not_echo_source_or_private_key(tmp_path, capsys):
    root = workspace(tmp_path / "science")
    (root / "manifest.json").write_text('{"files": ["SECRET-INPUT/../escape"]}')
    args = argparse.Namespace(directory=str(root), file_action="stage", manifest="manifest.json", paths=[])
    with pytest.raises(SystemExit) as error:
        main(args)
    assert "SECRET-INPUT" not in str(error.value) and TOKEN not in str(error.value)
    assert capsys.readouterr().out == ""


@pytest.mark.django_db(transaction=True)
def test_real_http_cli_stages_prepares_idempotently_and_fetches_without_executing_source(broker, context_live_server, tmp_path):
    c = broker
    c.credential.token_hash = token_hash(TOKEN)
    c.credential.save(update_fields=["token_hash"])
    root = workspace(tmp_path / "science", context_live_server.url)
    source = b"raise RuntimeError('BROKER-MUST-NOT-EXECUTE-THIS-SOURCE')\n"
    (root / "main.py").write_bytes(source)
    def cli(*arguments):
        result = subprocess.run([sys.executable, "-m", "compute_relay.cli", "files", *arguments,
            "--directory", str(root)], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert TOKEN not in result.stdout + result.stderr and source.decode().strip() not in result.stdout + result.stderr
        return json.loads(result.stdout)
    staged = cli("stage", "main.py")
    assert cli("stage", "main.py")["reused_objects"] == 1
    request = {**prepared_job(c).request}
    request.pop("project_id")
    request.pop("files")
    request["name"] = "workspace-transfer-acceptance"
    (root / "job.json").write_text(json.dumps(request))
    first = cli("prepare", "--bundle", staged["bundle_id"], "--request", "job.json")
    second = cli("prepare", "--bundle", staged["bundle_id"], "--request", "job.json")
    assert first["id"] == second["id"]
    assert first["next_action"]["action"] == "request_execution"
    assert "manifest" not in first and "files" not in first
    assert not Attempt.objects.exists() and not ExecutionRequest.objects.exists()
    job = Job.objects.get(id=first["id"])
    budgeted(c)
    attempt = execution(c, job).attempt
    Artifact.objects.create(attempt=attempt, path="answer.txt", blob=store_blob(c.project, b"synthetic-result\n"))
    result = cli("fetch", "--attempt-id", str(attempt.id), "--output", "results/first")
    assert result["integrity"] == "verified"
    assert (root / "results/first/answer.txt").read_bytes() == b"synthetic-result\n"


def test_content_lookup_checks_scope_project_size_and_stored_integrity(broker):
    c = broker
    blob = store_blob(c.project, b"ours")
    foreign = Project.objects.create(owner=c.outsider, name="foreign")
    other = store_blob(foreign, b"other")
    client = Client(HTTP_AUTHORIZATION="Bearer " + c.token)
    path = f"/api/v1/projects/{c.project.id}/objects/lookup"
    entries = [{"sha256": item.sha256, "size_bytes": item.size_bytes} for item in [blob, other]]
    def post(body, url=path):
        return client.post(url, data=json.dumps(body), content_type="application/json")
    response = post({"objects": entries})
    assert response.status_code == 200 and response.json() == {"present": entries[:1]}
    assert "no-store" in response["Cache-Control"] and c.token.encode() not in response.content
    assert post({"objects": entries}, f"/api/v1/projects/{foreign.id}/objects/lookup").status_code == 404
    assert post({"objects": [entries[0], entries[0]]}).status_code == 400
    assert post({"objects": [{**entries[0], "size_bytes": True}]}).status_code == 400
    assert post({"objects": [{**entries[0], "size_bytes": MAX_SOURCE_BYTES + 1}]}).status_code == 400
    assert post({"objects": []}).status_code == 400
    assert post({"objects": entries, "extra": True}).status_code == 400
    c.credential.scopes = ["jobs:read"]
    c.credential.save(update_fields=["scopes"])
    assert post({"objects": entries}).status_code == 403
    c.credential.scopes = ["objects:write"]
    c.credential.save(update_fields=["scopes"])
    from compute_relay.control.storage import blob_path
    blob_path(blob).write_bytes(b"oops")
    corrupted = post({"objects": entries[:1]})
    assert corrupted.status_code == 409 and corrupted.json()["error"]["code"] == "OBJECT_INTEGRITY"
    assert b"oops" not in corrupted.content
    blob_path(blob).unlink()
    missing = post({"objects": entries[:1]})
    assert missing.status_code == 409 and missing.json()["error"]["code"] == "OBJECT_UNAVAILABLE"
    assert str(blob_path(blob)).encode() not in missing.content


def test_attempt_accounting_distinguishes_requested_allocated_and_actual(broker):
    from compute_relay.control.services import serialize_attempt
    budgeted(broker)
    attempt = execution(broker, prepared_job(broker)).attempt
    attempt.state = "succeeded"
    attempt.accounting_state = "settled"
    attempt.observation = {"native_state": "COMPLETED", "exit_code": "0:0", "allocated_cpus": 2,
        "allocated_gpus": 0, "elapsed_seconds": 7, "private": "must-not-leak",
        "accounting": {"final": True, "cpu_core_seconds": 14, "gpu_device_seconds": 0}}
    result = serialize_attempt(attempt)
    assert result["execution_request_id"] == str(attempt.execution_request_id)
    assert result["accounting"]["requested"]["cpu_cores"] == 1
    assert result["accounting"]["allocated"]["cpu_cores"] == 2
    assert result["accounting"]["prediction"] == {"cpu_cores": 1, "gpu_devices": 0, "allocation_exceeded": True}
    assert result["accounting"]["cpu_core_seconds"] == 14
    assert result["accounting"]["reservation_difference"]["cpu_core_seconds"] == -46
    assert "must-not-leak" not in json.dumps(result)
    attempt.accounting_state = "settlement_pending"
    result = serialize_attempt(attempt)["accounting"]
    assert result["final"] is False and result["cpu_core_seconds"] is None
    attempt.accounting_state = "released"
    assert serialize_attempt(attempt)["accounting"]["cpu_core_seconds"] == 0
