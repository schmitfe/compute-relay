#!/usr/bin/env python3
"""Exercise a synthetic broker installation through its public HTTP interface.

Reads only explicitly supplied demo credentials; never opens SSH keys or cluster
configuration. Slurm executes on the disposable fixture; fake results are labeled.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from html.parser import HTMLParser
import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4


class SmokeFailure(Exception):
    """A safe-to-display description that never includes response bodies/secrets."""


class LocalRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        self.origin = origin

    def redirect_request(self, request, response, code, message, headers, new_url):
        target = urllib.parse.urlsplit(new_url)
        if (target.scheme, target.netloc) != self.origin:
            raise SmokeFailure("API attempted a redirect outside the selected local origin")
        return super().redirect_request(request, response, code, message, headers, new_url)


class Client:
    def __init__(self, base_url, *, token=None):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.hostname not in {"control", "localhost", "127.0.0.1", "::1"}
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise SmokeFailure("Smoke base URL must select the local fixture origin")
        self.base = base_url.rstrip("/")
        self.token = token
        self.last_headers = {}
        self.last_status = None
        self.last_url = None
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies),
            LocalRedirects((parsed.scheme, parsed.netloc)),
            urllib.request.ProxyHandler({}),
        )

    def cookie(self, name):
        values = [cookie.value for cookie in self.cookies if cookie.name == name]
        if len(values) != 1:
            raise SmokeFailure(f"Expected one {name} cookie from the local application")
        return values[0]

    def request(self, method, path, payload=None, *, expected=(200, 201, 202), binary=False, form=False):
        if not path.startswith("/") or path.startswith("//"):
            raise SmokeFailure("Client rejected an invalid local API path")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if payload is not None:
            if form:
                data = urllib.parse.urlencode(payload, doseq=True).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(payload, allow_nan=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["Idempotency-Key"] = str(uuid4())
            headers["Origin"] = self.base
            headers["Referer"] = self.base + "/login/"
            csrf = [cookie.value for cookie in self.cookies if cookie.name == "csrftoken"]
            if csrf:
                headers["X-CSRFToken"] = csrf[-1]
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.status
            self.last_status = status
            self.last_headers = {name.lower(): value for name, value in response.headers.items()}
            self.last_url = response.geturl()
            raw = response.read(5 * 1024 * 1024 + 1)
        if status not in expected:
            raise SmokeFailure(f"{method} {path} returned HTTP {status}")
        if len(raw) > 5 * 1024 * 1024:
            raise SmokeFailure("Application response exceeded smoke-client limit")
        if binary:
            return raw
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise SmokeFailure(f"{method} {path} did not return valid JSON") from None

    def login(self, username, password):
        self.request("GET", "/login/", binary=True)
        self.request("POST", "/login/", {
            "username": username, "password": password,
            "csrfmiddlewaretoken": self.cookie("csrftoken"),
        }, binary=True, form=True)
        self.cookie("sessionid")


def require(condition, message):
    if not condition:
        raise SmokeFailure(message)


class PreformattedText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag == "pre":
            self.active = {"id": dict(attrs).get("id"), "text": ""}

    def handle_data(self, text):
        if self.active is not None:
            self.active["text"] += text

    def handle_endtag(self, tag):
        if tag == "pre" and self.active is not None:
            self.blocks.append(self.active)
            self.active = None


def revealed_key(human, html):
    require("no-store" in human.last_headers.get("cache-control", ""), "Key response permits caching")
    parsed = PreformattedText()
    parsed.feed(html.decode("utf-8"))
    values = [block["text"].strip() for block in parsed.blocks if block["id"] == "api-secret"]
    require(len(values) == 1 and values[0].startswith("cbp_"), "Key creation did not reveal one project secret")
    match = re.search(rb"Identifier:\s*([0-9a-f-]{36})", html)
    require(match is not None, "Key creation response lacks its public identifier")
    return values[0], str(UUID(match.group(1).decode()))


def create_job(agent, demo, backend, source, name, *, previous_job_id=None):
    descriptor = agent.request(
        "POST", f"/api/v1/projects/{demo['project_id']}/objects",
        {"path": "main.py", "content_base64": base64.b64encode(source).decode("ascii")},
    )
    require(descriptor["sha256"] == hashlib.sha256(source).hexdigest(), "Uploaded object hash mismatch")
    request = {
        "schema_version": "1.0", "project_id": demo["project_id"],
        "connection_id": demo["connection_id"], "name": name,
        "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
        "environment": {"id": demo["environment_id"], "revision_sha256": demo["environment_revision_sha256"]},
        "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
        "slurm": {"partition": "debug", "account": "broker"},
        "files": [descriptor], "outputs": ["result.json"],
    }
    create_path = f"/api/v1/jobs/{previous_job_id}/revisions" if previous_job_id else "/api/v1/jobs"
    job = agent.request("POST", create_path, request)
    prepared = agent.request("POST", f"/api/v1/jobs/{job['id']}/prepare", {})
    execution = agent.request("POST", f"/api/v1/jobs/{job['id']}/execution-requests", {})
    require(not execution.get("attempt_id"), "Review-mode request ran before human approval")
    return job["id"], prepared["manifest_sha256"], execution["id"]


def run(base_url, demo_file, timeout):
    demo = json.loads(demo_file.read_bytes())
    token_file = demo_file.parent / "agent-token"
    token = token_file.read_text().strip() if token_file.exists() else demo.get("agent_token")
    require(isinstance(token, str) and 16 <= len(token) <= 4096, "Missing synthetic project token")
    agent = Client(base_url, token=token)
    human = Client(base_url)
    human.login(demo["owner_username"], demo["owner_password"])
    inventory = agent.request("GET", "/api/v1/connections")["connections"]
    connection = next((item for item in inventory if item["id"] == demo["connection_id"]), None)
    require(connection is not None, "Bootstrapped connection is absent from project inventory")
    backend = connection["backend"]
    require(backend in {"fake", "slurm"}, "This smoke supports only fake or disposable Slurm")

    marker = "BROKER_CONTAINER_SMOKE_OK"
    source = (
        "import json\nimport time\nfrom pathlib import Path\n"
        f"print({marker!r}, flush=True)\n"
        "time.sleep(1.1)\n"
        "Path('result.json').write_text(json.dumps({'ok': True, 'revision': 2}))\n"
    ).encode()
    suffix = uuid4().hex[:12]
    denied_id, denied_digest, denied_execution = create_job(
        agent, demo, backend, b"# initial proposal\n" + source, "smoke-denied-" + suffix,
    )
    decision_path = f"/api/v1/jobs/{denied_id}/decisions"
    agent.request("POST", decision_path, {
        "execution_request_id": denied_execution, "manifest_sha256": denied_digest,
        "approved": True, "comment": "Agent credential must not approve",
    }, expected=(401, 403), binary=True)
    rejection_comment = "Please revise the package before executing this test."
    human.request("POST", decision_path, {
        "execution_request_id": denied_execution, "manifest_sha256": denied_digest,
        "approved": False, "comment": rejection_comment,
    })
    denied = agent.request("GET", f"/api/v1/jobs/{denied_id}")
    require(not denied.get("attempts"), "Denied request created an execution attempt")
    require(rejection_comment in json.dumps(denied), "Agent cannot retrieve human rejection comment")
    require(denied["state"] == "rejected" and denied.get("package_state") == "prepared",
            "Rejected execution is still displayed as prepared")

    job_id, digest, execution_id = create_job(
        agent, demo, backend, b"# revised proposal\n" + source, "smoke-approved-" + suffix,
        previous_job_id=denied_id,
    )
    require(digest != denied_digest, "Revised executable bytes did not change the prepared digest")
    decision = human.request("POST", f"/api/v1/jobs/{job_id}/decisions", {
        "execution_request_id": execution_id, "manifest_sha256": digest,
        "approved": True, "comment": "Approve this immutable synthetic package once.",
    })
    attempt_id = decision.get("attempt_id")
    require(attempt_id, "Human approval did not produce an execution attempt")
    deadline = time.monotonic() + timeout
    final = None
    while time.monotonic() < deadline:
        job = agent.request("GET", f"/api/v1/jobs/{job_id}")
        attempt = next((item for item in job.get("attempts", []) if item["id"] == attempt_id), None)
        require(attempt is not None, "Approved execution attempt disappeared")
        state = attempt["state"].lower()
        if state in {"failed", "cancelled", "timeout", "rejected", "quarantined"}:
            raise SmokeFailure(f"Execution reached unexpected state {state}")
        if state in {"completed", "succeeded"} and attempt.get("accounting_state") == "settled":
            final = attempt
            break
        time.sleep(1)
    require(final is not None, "Execution and final accounting did not complete before smoke deadline")
    require(job["state"] in {"completed", "succeeded"} and job.get("package_state") == "prepared",
            "Completed execution is still displayed as prepared")
    require(job.get("name") == "smoke-approved-" + suffix and job.get("request", {}).get("name") == job["name"],
            "Agent-supplied job name was not preserved")
    require(job.get("owner", {}).get("username") == demo["owner_username"] and job.get("submitted_using", {}).get("name"),
            "Job API lacks owner or creating-key attribution")
    logs = agent.request("GET", f"/api/v1/attempts/{attempt_id}/logs")
    listed = agent.request("GET", f"/api/v1/attempts/{attempt_id}/artifacts")["artifacts"]
    result = next((item for item in listed if item["path"] == "result.json"), None)
    require(result is not None, "Declared result artifact is missing")
    content = agent.request("GET", f"/api/v1/artifacts/{result['id']}/content", binary=True)
    require(hashlib.sha256(content).hexdigest() == result["sha256"], "Collected artifact hash mismatch")
    result_data = json.loads(content)
    if backend == "fake":
        require("uploaded code was not executed" in logs["stdout"], "Fake output is not labeled as simulated")
        require(result_data.get("simulation") is True and result_data.get("executed_uploaded_code") is False,
                "Fake result does not disclose simulated execution")
    else:
        require(marker in logs["stdout"], "Real Slurm job did not produce the expected output marker")
        require(result_data == {"ok": True, "revision": 2}, "Real Slurm result payload differs")
    budget = agent.request("GET", f"/api/v1/projects/{demo['project_id']}/allowance")
    require(budget["reserved"] == {"cpu_core_seconds": 0, "gpu_device_seconds": 0},
            "Completed isolated demo left an outstanding compute reservation")
    require(budget["spent"]["cpu_core_seconds"] > 0 and budget["spent"]["gpu_device_seconds"] == 0,
            "Final allocated CPU usage was not charged as expected")
    require(budget["debt"] == {"cpu_core_seconds": 0, "gpu_device_seconds": 0},
            "Tiny demo exceeded its authorized compute envelope")
    native_job_id = str(final.get("native_identity", {}).get("job_id", ""))
    require(native_job_id.isdigit(), "Completed attempt lacks its native job identifier")
    print(json.dumps({"ok": True, "backend": backend, "simulated": backend == "fake",
                      "rejected_job_id": denied_id, "job_id": job_id, "attempt_id": attempt_id,
                      "native_job_id": native_job_id, "accounting_state": final["accounting_state"],
                      "spent": budget["spent"], "reserved": budget["reserved"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--demo-file", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 900:
        parser.error("timeout must be from 1 through 900 seconds")
    try:
        run(args.base_url, args.demo_file, args.timeout)
    except SmokeFailure as error:
        print("Smoke failed: " + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as error:
        # Do not serialize errors/response bodies that could contain credentials.
        print(f"Smoke failed: client/protocol error ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
