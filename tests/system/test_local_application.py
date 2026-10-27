"""A real local command starts, stops and reuses its own SQLite installation."""

import http.cookiejar
import json
import os
import re
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from container_smoke import Client


def environment():
    result = {key: value for key, value in os.environ.items()
        if not key.startswith("BROKER_") and key not in {"DJANGO_SETTINGS_MODULE", "PYTHONPATH"}}
    result["PYTHONUNBUFFERED"] = "1"
    return result


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def start(root, args):
    log = root / ("launch-" + str(time.monotonic_ns()) + ".log")
    with log.open("w") as stream:
        process = subprocess.Popen([sys.executable, "-I", "-m", "compute_relay", *args],
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, cwd=root, env=environment())
    return process, log


def ready(process, origin, log):
    deadline = time.monotonic() + 30
    client = opener()
    while time.monotonic() < deadline:
        assert process.poll() is None, log.read_text()
        try:
            with client.open(origin + "/healthz", timeout=0.5) as response:
                result = json.loads(response.read())
            assert result["profile"] == "local" and result["status"] == "ok"
            return client, result
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    pytest.fail("Local command did not become ready: " + log.read_text())


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            assert process.wait(timeout=15) == 0
        except subprocess.TimeoutExpired:
            process.kill();process.wait()
            pytest.fail("Local command did not cleanly stop after Ctrl-C")


def test_default_command_password_free_supervisor_and_same_state_restart(tmp_path):
    port = free_port();origin = f"http://127.0.0.1:{port}"
    state = tmp_path / "state"
    # Deliberately omit --no-connectors: the real supervisor must remain running
    # and watch this new installation without contacting any cluster.
    argv = ["--data-directory", str(state), "--port", str(port), "--no-password", "--no-browser", "--non-interactive"]
    process, log = start(tmp_path, argv)
    try:
        client, first = ready(process, origin, log)
        # A different local OS user can reach loopback but has no launch token.
        with client.open(origin + "/", timeout=3) as response:
            assert "/login/" in response.url
        deadline = time.monotonic() + 5
        while "Portal access" not in log.read_text() and time.monotonic() < deadline:
            time.sleep(.05)
        launch_url = re.search(r"http://[^\s]+/login/\?launch_token=[^\s]+", log.read_text()).group()
        with client.open(launch_url, timeout=3) as response:
            assert response.url == origin + "/" and b"Compute Relay" in response.read()
        with opener().open(launch_url, timeout=3) as response:
            assert "/login/" in response.url
        with pytest.raises(urllib.error.HTTPError) as denied:
            client.open(origin + "/api/v1/context", timeout=3)
        assert denied.value.code == 401
        request = urllib.request.Request(origin + "/projects/new/", data=b"name=unsafe")
        with pytest.raises(urllib.error.HTTPError) as denied:
            client.open(request, timeout=3)
        assert denied.value.code == 403
        duplicate = subprocess.run([sys.executable, "-I", "-m", "compute_relay", *argv], cwd=tmp_path,
            env=environment(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        assert duplicate.returncode == 0 and "already running" in duplicate.stdout
        config = json.loads((state / "installation.json").read_text())
        with sqlite3.connect(state / "control.sqlite3") as db:
            owner = db.execute("SELECT id,password FROM broker_user").fetchall()
        assert len(owner) == 1 and owner[0][0] == config["owner_id"].replace("-", "")
        assert owner[0][1].startswith("!")
    finally:
        stop(process)
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    # Omit the access-mode and port flags: both come from saved state.
    process, log = start(tmp_path, ["start", "--data-directory", str(state), "--no-browser", "--non-interactive"])
    try:
        client, second = ready(process, origin, log)
        assert first["installation_id"] == second["installation_id"]
        assert first["instance_id"] != second["instance_id"]
        assert json.loads((state / "installation.json").read_text()) == config
        with sqlite3.connect(state / "control.sqlite3") as db:
            assert db.execute("SELECT id,password FROM broker_user").fetchall() == owner
    finally:
        stop(process)


def test_password_setup_and_browser_session_survive_restart_without_secret_files(tmp_path):
    password = "synthetic-local-password-do-not-log"
    password_file = tmp_path / "supplied-password"
    password_file.write_text(password);password_file.chmod(0o600)
    state = tmp_path / "state";port = free_port();origin = f"http://127.0.0.1:{port}"
    argv = ["--data-directory", str(state), "--port", str(port), "--owner-password-file", str(password_file),
        "--no-browser", "--no-connectors", "--non-interactive"]
    process, log = start(tmp_path, argv)
    browser = Client(origin)
    try:
        client, first = ready(process, origin, log)
        with client.open(origin + "/", timeout=3) as response:
            assert "/login/" in response.url
        browser.login("local", password)
        assert b"Compute Relay" in browser.request("GET", "/", binary=True)
        for path in state.glob("*.json"):
            assert password not in path.read_text()
        for path in state.glob("*.log"):
            assert password not in path.read_text()
        assert password not in log.read_text()
    finally:
        stop(process)
    password_file.unlink()
    process, log = start(tmp_path, ["--data-directory", str(state), "--no-browser", "--no-connectors", "--non-interactive"])
    try:
        _, second = ready(process, origin, log)
        assert first["installation_id"] == second["installation_id"]
        assert b"Compute Relay" in browser.request("GET", "/", binary=True)
    finally:
        stop(process)


def test_port_conflict_does_not_initialize_or_replace_another_installation(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0));listener.listen()
        port = listener.getsockname()[1]
        state = tmp_path / "state"
        result = subprocess.run([sys.executable, "-I", "-m", "compute_relay", "--data-directory", str(state),
            "--port", str(port), "--no-password", "--no-browser", "--no-connectors", "--non-interactive"],
            cwd=tmp_path, env=environment(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        assert result.returncode == 1 and "already in use" in result.stderr
        assert not (state / "control.sqlite3").exists() and not (state / "installation.json").exists()


def test_offline_maintenance_cli_preserves_identity_and_never_replays_compacted_requests(tmp_path):
    state = tmp_path / "state"
    port = free_port()
    process, log = start(tmp_path, ["--data-directory", str(state), "--port", str(port),
        "--extensions", "", "--no-password", "--no-browser", "--no-connectors", "--non-interactive"])

    def command(*arguments):
        return subprocess.run([sys.executable, "-I", "-m", "compute_relay", *arguments,
            "--data-directory", str(state)], cwd=tmp_path, env=environment(),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

    try:
        ready(process, f"http://127.0.0.1:{port}", log)
        result = command("maintain-state", "--apply")
        assert result.returncode == 1 and "Stop Compute Relay" in result.stderr
    finally:
        stop(process)

    # Populate only this disposable installation. This uses the same bounded
    # metadata APIs as admission, without a cluster, key, or execution request.
    seed = '''
import os, sys
from datetime import timedelta
from uuid import uuid4
from compute_relay.local_state import LocalState
from compute_relay.local_runtime import service_environment
with LocalState(sys.argv[1], create=False) as state:
    os.environ.update(service_environment(state, state.read("installation.json"), str(uuid4())))
os.environ["DJANGO_SETTINGS_MODULE"] = "compute_relay.control.settings"
import django
django.setup()
from django.conf import settings
from django.utils import timezone
from compute_relay.control import services
from compute_relay.control.metadata import json_size, reserve_metadata
from compute_relay.control.models import IdempotencyRecord, Job, Project, User
owner = User.objects.get(id=settings.BROKER_LOCAL_OWNER_ID)
project = Project.objects.create(owner=owner, name="offline maintenance fixture")
request = {"name":"old draft", "connection_id":str(uuid4()), "arguments":"x" * 65536}
job = Job.objects.create(project=project, request=request)
reserve_metadata(project, "job", job.id, 32768 + 2 * json_size(request))
Job.objects.filter(id=job.id).update(created_at=timezone.now() - timedelta(days=60))
services.idempotent(project, "fixture", "request-once", {}, lambda: ({"id":str(job.id), "receipt":"x" * 4096}, 201))
IdempotencyRecord.objects.update(created_at=timezone.now() - timedelta(days=60))
from compute_relay.control import storage
# Crash after durable publication but before Blob/counter finalization. The OS
# releases the writer lock, while SQLite retains the committed reservation.
storage._finish = lambda *args, **kwargs: os._exit(88)
storage.store_blob(project, b"crash-published fixture")
'''
    seeded = subprocess.run([sys.executable, "-I", "-c", seed, str(state)], cwd=tmp_path,
        env=environment(), capture_output=True, text=True, timeout=20)
    assert seeded.returncode == 88, seeded.stderr
    with sqlite3.connect(state / "control.sqlite3") as database:
        assert database.execute("SELECT count(*) FROM broker_blob").fetchone()[0] == 0
        assert database.execute("SELECT state FROM broker_uploadreservation").fetchone()[0] == "reserved"
        # Advance only the recovery eligibility clock, without waiting 15 minutes.
        database.execute("UPDATE broker_uploadreservation SET expires_at='2000-01-01 00:00:00'")
    before_config = (state / "installation.json").read_bytes()
    flags = ["maintain-state", "--archive-drafts", "--compact-idempotency", "--limit", "1"]
    dry = command(*flags)
    assert dry.returncode == 0, dry.stderr
    preview = json.loads(dry.stdout)
    assert preview["applied"] is False and preview["draft_jobs"]["candidates"] == 1
    assert preview["expired_uploads"] == 1
    assert preview["metadata_before"] == preview["metadata_after"]
    applied = command(*flags, "--apply")
    assert applied.returncode == 0, applied.stderr
    result = json.loads(applied.stdout)
    assert result["draft_jobs"]["archived"] == 1 and result["idempotency_compacted"] == 1
    assert result["upload_recovery"] == {"recovered": 1, "busy": 0, "failed": 0}
    assert result["metadata_after"]["logical_bytes"] < result["metadata_before"]["logical_bytes"]
    with sqlite3.connect(state / "control.sqlite3") as database:
        job = database.execute("SELECT state,request FROM broker_job").fetchone()
        assert job[0] == "archived" and set(json.loads(job[1])) == {"name", "connection_id"}
        assert database.execute("SELECT compacted,status,response FROM broker_idempotencyrecord").fetchone() == (1, 409, "{}")
        assert database.execute("SELECT storage_used,storage_reserved,object_count FROM broker_project").fetchone() == (len(b"crash-published fixture"), 0, 1)
        assert database.execute("SELECT used,reserved FROM broker_storagepool").fetchone() == (len(b"crash-published fixture"), 0)
        assert database.execute("SELECT count(*) FROM broker_uploadgrant").fetchone()[0] == 0
    repeated = command(*flags, "--apply")
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["draft_jobs"]["archived"] == 0
    assert json.loads(repeated.stdout)["upload_recovery"]["recovered"] == 0
    assert (state / "installation.json").read_bytes() == before_config
    checked = command("check-upgrade")
    assert checked.returncode == 0 and json.loads(checked.stdout)["management_compatible"] is True
