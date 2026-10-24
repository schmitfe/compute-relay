"""A real local command starts, stops and reuses its own SQLite installation."""

import http.cookiejar
import json
import os
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
        with client.open(origin + "/", timeout=3) as response:
            assert response.url == origin + "/" and b"Compute Relay" in response.read()
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
