"""Exercise the native acceptance monitor against a real loopback HTTP server."""

import json
import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from container_smoke import SmokeFailure
from personal_connector_smoke import PortalProbes
import personal_connector_smoke as smoke


@pytest.fixture
def portal():
    calls = []
    failing = threading.Event()
    slow = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((self.path, self.headers.get("Cookie"), self.headers.get("Authorization")))
            if slow.is_set():
                time.sleep(.2)
            self.send_response(503 if failing.is_set() else 200)
            self.end_headers()
            try:
                self.wfile.write(json.dumps({"revision": "a" * 64}).encode())
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(base=f"http://127.0.0.1:{server.server_port}", calls=calls, failing=failing, slow=slow)
    server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_portal_probe_checks_ordinary_pages_and_bounded_wait_during_work(portal):
    human = SimpleNamespace(cookie=lambda _: "synthetic-session")
    agent = SimpleNamespace(token="synthetic-project-token")
    with PortalProbes(portal.base, human, agent, "project", "job") as probes:
        deadline = time.monotonic() + 3
        while not {"/healthz", "/projects/project/", "wait"} <= probes.paths and time.monotonic() < deadline:
            time.sleep(.01)
    assert probes.count >= 4
    assert any(path == "/healthz" for path, _, _ in portal.calls)
    assert any(path == "/projects/project/" and cookie == "sessionid=synthetic-session"
               for path, cookie, _ in portal.calls)
    assert any("wait_seconds=1" in path and "after_revision=" in path
               and token == "Bearer synthetic-project-token" for path, _, token in portal.calls)


def test_portal_probe_fails_if_a_page_stalls_or_returns_an_error(portal):
    human = SimpleNamespace(cookie=lambda _: "synthetic-session")
    agent = SimpleNamespace(token="synthetic-project-token")
    portal.failing.set()
    with pytest.raises(SmokeFailure, match="Portal probe failed"):
        with PortalProbes(portal.base, human, agent, "project", "job") as probes:
            probes.stop.wait(3)


def test_fault_child_loses_only_one_accepted_terminal_reply(tmp_path, monkeypatch):
    events = []

    async def accepted(*args, **kwargs):
        events.append("accepted")
        return {"accepted": True}

    monkeypatch.setattr(smoke.os, "_exit", lambda code: events.append(code))
    (tmp_path / "arm-terminal").touch()
    report = {"outcome": "observed", "observation": {"terminal": True}}
    asyncio.run(smoke.fault_report(accepted, tmp_path, "POST", "/internal/v1/attempts/id/report", report))
    asyncio.run(smoke.fault_report(accepted, tmp_path, "POST", "/internal/v1/attempts/id/report", report))
    assert events == ["accepted", 73, "accepted"]
    assert (tmp_path / "lost-terminal-reply").exists()


def test_portal_probe_timeout_fails_acceptance(portal):
    portal.slow.set()
    with pytest.raises(SmokeFailure, match="capture.*thread stacks"):
        with PortalProbes(portal.base, SimpleNamespace(cookie=lambda _: "session"),
                SimpleNamespace(token="token"), "project", "job", timeout=.05) as probes:
            probes.stop.wait(3)


def test_fault_child_refuses_an_institutional_target(tmp_path):
    setup = tmp_path / "synthetic-setup.json"
    setup.write_text(json.dumps({"target": {"host": "institution.invalid", "user": "human"}}))
    with pytest.raises(SmokeFailure, match="disposable fixture"):
        smoke.fault_connector(tmp_path, ["connector", "--state-directory", str(tmp_path / "state"), "--setup", str(setup)])


def test_cluster_check_waits_for_collection_before_reading_diagnostic_json(monkeypatch):
    connection = "11111111-1111-4111-8111-111111111111"
    environment = "22222222-2222-4222-8222-222222222222"
    test_path = f"/clusters/{connection}/test/"
    environment_path = f"/clusters/{connection}/environments/new/"
    review_path = "/review/job/"

    class Human:
        last_url = "http://control:8765/"
        approved = False
        reviews = 0
        result_reads = 0

        def request(self, method, path, payload=None, **kwargs):
            if path == environment_path and method == "GET":
                return b'<input name="connection_revision" value="1">'
            if path == environment_path and method == "POST":
                if payload.get("action") == "inspect":
                    self.last_url = "http://control:8765" + environment_path + "?task=inspection"
                return b""
            if path.endswith("?task=inspection"):
                return b'Compute-node availability is unverified<option value="/usr/bin/python3">'
            if path == test_path and method == "POST":
                self.last_url = "http://control:8765" + review_path
                return b'compute-relay-cluster-check-v1<input name="execution_request_id" value="execution">'
            if path == review_path and method == "POST":
                self.approved = True
                return b""
            if path == review_path:
                self.reviews += 1
                collection = "pending" if self.reviews == 1 else "complete"
                return ('<section data-state="succeeded" data-accounting-state="settled">'
                    '<dt>Output collection</dt><dd>' + collection + '</dd></section>').encode()
            if path == test_path and not self.approved:
                return (f'Prepare test for review<option value="{environment}">Fixture cluster modules</option>').encode()
            if path == test_path:
                self.result_reads += 1
                if self.reviews < 2:
                    return b"Diagnostic output has not been collected."
                return b'<pre>{"marker":"compute-relay-cluster-check-v1","hostname":"node","python":"3.13","loaded_modules":"fixture-python/1.0"}</pre>'
            raise AssertionError((method, path))

    monkeypatch.setattr(smoke.time, "sleep", lambda _: None)
    human = Human()
    smoke.cluster_check(human, {"connection_id": connection})
    assert human.reviews == 2 and human.result_reads == 1
