#!/usr/bin/env python3
"""Synthetic Docker-only personal connector acceptance; no institutional keys."""

import argparse
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time
import threading
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from container_smoke import Client, PreformattedText, SmokeFailure, create_job, revealed_key, require, run as job_smoke


async def fault_report(request, directory, method, path, body=None):
    reply = await request(method, path, body)
    if (path.endswith("/report") and body.get("outcome") == "observed"
            and body.get("observation", {}).get("terminal") is True
            and "collection_state" not in body and (directory / "arm-terminal").exists()
            and not (directory / "lost-terminal-reply").exists()):
        (directory / "lost-terminal-reply").touch()
        os._exit(73)  # Preserve the real ready journal after the broker accepted the report.
    return reply


def fault_connector(directory, arguments):
    """Inject controlled faults only into this disposable connector child."""
    from compute_relay.backends import SlurmBackend
    from compute_relay.worker.async_client import AsyncJsonClient
    from compute_relay.cli import main
    state_directory = Path(arguments[arguments.index("--state-directory") + 1])
    state = json.loads((state_directory / "state.json").read_bytes()) if (state_directory / "state.json").exists() else \
        json.loads(Path(arguments[arguments.index("--setup") + 1]).read_bytes())
    target = state["connection"]["ssh"] if "connection" in state else state["target"]
    require(target.get("host") == "slurm-fixture" and target.get("user") == "brokerjob",
        "Fault child requires the disposable fixture")
    original_request, original_chunk, original_submit = AsyncJsonClient.request, SlurmBackend.output_chunk, SlurmBackend.submit

    async def request(self, method, path, body=None):
        return await fault_report(lambda *args: original_request(self, *args), directory, method, path, body)

    async def output_chunk(self, native, **kwargs):
        result = await original_chunk(self, native, **kwargs)
        if kwargs["offset"] >= 1024 * 1024 and not (directory / "collection-interrupted").exists():
            (directory / "collection-interrupted").touch()
            async with asyncio.timeout(20):
                while not (directory / "resume-collection").exists():
                    await asyncio.sleep(.05)
        return result

    async def submit(self, attempt_id, *args, **kwargs):
        with (directory / "submissions").open("a") as stream:
            stream.write(str(attempt_id) + "\n")
        return await original_submit(self, attempt_id, *args, **kwargs)

    AsyncJsonClient.request, SlurmBackend.output_chunk, SlurmBackend.submit = request, output_chunk, submit
    sys.argv = [sys.argv[0], *arguments]
    main()


class PortalProbes:
    """Probe ordinary pages while an authenticated bounded wait occupies a thread."""
    def __init__(self, base, human, agent, project_id, job_id, *, timeout=3):
        self.base, self.timeout = base, timeout
        self.session, self.token = human.cookie("sessionid"), agent.token
        self.project_id, self.job_id = project_id, job_id
        self.stop = threading.Event()
        self.samples, self.errors, self.paths = [], [], set()

    @property
    def count(self):
        return len(self.samples)

    def _run(self, waiting):
        try:
            with httpx.Client(base_url=self.base, timeout=self.timeout, trust_env=False,
                    cookies={"sessionid": self.session},
                    headers={"Authorization": "Bearer " + self.token} if waiting else {}) as http:
                revision = None
                while not self.stop.is_set():
                    paths = [f"/api/v1/jobs/{self.job_id}"] if waiting else ["/healthz", f"/projects/{self.project_id}/"]
                    for path in paths:
                        query = {"after_revision": revision, "wait_seconds": 1} if waiting and revision else {}
                        started = time.monotonic()
                        response = http.get(path, params=query)
                        require(response.status_code == 200, f"Portal probe failed ({response.status_code})")
                        self.samples.append(time.monotonic() - started)
                        self.paths.add("wait" if query else path)
                        if waiting:
                            revision = response.json()["revision"]
                    self.stop.wait(.05)
        except Exception as error:
            self.errors.append(type(error).__name__)
            self.stop.set()

    def __enter__(self):
        self.threads = [threading.Thread(target=self._run, args=(waiting,), daemon=True) for waiting in (False, True)]
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, kind, error, traceback):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=self.timeout + 1)
        if kind is None:
            require(not self.errors, "Portal probe failed; capture the control service thread stacks before cleanup")
            require({"/healthz", f"/projects/{self.project_id}/", "wait"} <= self.paths,
                "Portal ordinary-page and bounded-wait probes did not all execute")


class ConfigurationFields(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name"):
            self.values[values["name"]] = values.get("value", "")


class ClusterCheckFields(ConfigurationFields):
    def __init__(self):
        super().__init__()
        self.selected = None

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        values = dict(attrs)
        if tag == "select":
            self.selected = values.get("name")
        elif tag == "option" and self.selected:
            if self.selected not in self.values or "selected" in values:
                self.values[self.selected] = values.get("value", "")

    def handle_endtag(self, tag):
        if tag == "select":
            self.selected = None


def redirected_path(client):
    parsed = urlsplit(client.last_url)
    return parsed.path + ("?" + parsed.query if parsed.query else "")


def finish_configuration(human, demo, directory, *, folder_name="personal-broker-jobs"):
    import re
    connection_id = demo["connection_id"]
    page = human.request("GET", f"/clusters/{connection_id}/configuration/", binary=True)
    fields = ConfigurationFields();fields.feed(page.decode())
    human.request("POST", f"/clusters/{connection_id}/directories/", {
        "path": "", "connection_revision": fields.values["connection_revision"]}, form=True, binary=True)
    pending = redirected_path(human)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        page = human.request("GET", pending, binary=True)
        match = re.search(rb'data-home="([^"]+)"', page)
        if match:
            home = unescape(match.group(1).decode())
            break
        time.sleep(0.25)
    else:
        raise SmokeFailure("Remote directory listing timed out")
    # Create one folder through the narrow owner operation, then explicitly
    # select it as the baseline. Creation alone must not change worker scope.
    chosen = home + "/" + folder_name
    original_root = json.loads((directory / "state.json").read_bytes())["connection"]["remote_root"]
    human.request("POST", f"/clusters/{connection_id}/directories/", {
        "action": "mkdir", "path": home, "name": folder_name,
        "connection_revision": fields.values["connection_revision"]}, form=True, binary=True)
    pending = redirected_path(human)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        page = human.request("GET", pending, binary=True)
        match = re.search(rb'id="remote-directory" data-path="([^"]+)"', page)
        if match:
            require(unescape(match.group(1).decode()) == chosen, "New folder did not open for selection")
            require(b"Folder created." in page, "Folder creation was not acknowledged")
            break
        time.sleep(0.25)
    else:
        raise SmokeFailure("Remote folder creation timed out")
    require(json.loads((directory / "state.json").read_bytes())["connection"]["remote_root"] == original_root,
            "Creating a folder changed the selected baseline")
    page = human.request("GET", f"/clusters/{connection_id}/configuration/", binary=True)
    fields = ConfigurationFields();fields.feed(page.decode())
    fields.values["remote_root"] = chosen
    human.request("POST", f"/clusters/{connection_id}/configuration/", fields.values, form=True, binary=True)
    pending = redirected_path(human)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        page = human.request("GET", pending, binary=True)
        if b'id="setup-task" data-state="succeeded"' in page:
            break
        if b'id="setup-task" data-state="failed"' in page:
            raise SmokeFailure("Cluster configuration was rejected by its connector")
        time.sleep(0.25)
    else:
        raise SmokeFailure("Cluster configuration did not finish")
    saved = json.loads((directory / "state.json").read_bytes())
    require(saved["connection"]["remote_root"] == chosen, "Selected baseline was not persisted locally")


def master_pid(directory):
    import re
    state = json.loads((directory / "state.json").read_bytes())
    require(state["connection"]["ssh"]["host"] == "slurm-fixture", "SSH fault injection requires the disposable fixture")
    command = ["/usr/bin/ssh", "-F", "/dev/null", "-S", str(directory / "s"),
        "-o", "BatchMode=yes", "-o", "ProxyCommand=/bin/false", "-o", "IdentityFile=none",
        "-O", "check", "--", "brokerjob@slurm-fixture"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=False)
    match = re.search(rb"Master running \(pid=([0-9]+)\)", result.stderr)
    return int(match.group(1)) if result.returncode == 0 and match else None


def wait_for_master_change(directory, worker, original):
    state = json.loads((directory / "state.json").read_bytes())
    deadline = time.monotonic() + 95
    while time.monotonic() < deadline:
        require(worker.poll() is None, "Connector stopped instead of renewing its permitted SSH session")
        restored = master_pid(directory)
        if restored is not None and restored != original:
            require(json.loads((directory / "state.json").read_bytes())["worker_id"] == state["worker_id"],
                "SSH recovery replaced the broker worker identity")
            return
        time.sleep(.25)
    raise SmokeFailure("Connector did not renew its permitted SSH session")


def interrupt_master(directory, worker):
    """Abruptly kill only this fixture connector's owned local master."""
    original = master_pid(directory)
    require(original is not None and worker.poll() is None, "Fixture SSH master is not running")
    os.kill(original, signal.SIGKILL)
    wait_for_master_change(directory, worker, original)


def reconnect_policy(human, demo, enabled):
    page = human.request("GET", f"/clusters/{demo['connection_id']}/", binary=True)
    fields = ConfigurationFields(); fields.feed(page.decode())
    body = {"setup_revision": fields.values["setup_revision"]}
    if enabled:
        body["automatic_reconnect_enabled"] = "on"
    human.request("POST", f"/clusters/{demo['connection_id']}/connector-settings/", body, form=True, binary=True)


def parallel_dispatch(human, demo, base, directory, worker):
    """Eight native submissions before completion on a two-CPU fake institution."""
    require(demo.get("backend") == "slurm" and demo.get("cluster_form", {}).get("host") == "slurm-fixture",
        "Parallel dispatch acceptance requires the disposable Slurm fixture")
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    human.request("POST", "/projects/new/", {"name": "Parallel dispatch fixture", "mode": "approval_required",
        "cpu_hours": "2", "gpu_hours": "0", "max_cpus": "1", "max_gpus": "0", "memory_mib": "128",
        "wall_minutes": "1", "max_active_jobs": "10", "max_total_jobs": "8", "expires_at": expiry,
        "connections": [demo["connection_id"]], "environments": [demo["environment_id"]]}, form=True, binary=True)
    path = redirected_path(human)
    require(path.startswith("/projects/") and path != "/projects/new/", "Parallel project was not created")
    project_id = str(UUID(path.strip("/").split("/")[-1]))
    html = human.request("POST", f"/projects/{project_id}/keys/new/", {
        "name": "Parallel fixture agent", "expires_at": expiry, "cluster_access": "inherit",
        "execution_mode": "inherit", "submission_id": str(uuid4()),
        "scopes": ["jobs:read", "jobs:write", "jobs:execute", "artifacts:read", "objects:write"]}, form=True, binary=True)
    token, _ = revealed_key(human, html)
    agent = Client(base, token=token)
    selected = {**demo, "project_id": project_id}
    source = b'import time\nfrom pathlib import Path\ntime.sleep(30)\nPath("result.json").write_text("{\\"ok\\": true}")\n'
    proposals = [create_job(agent, selected, "slurm", source, f"parallel-fixture-{index}") for index in range(8)]
    for job_id, digest, execution_id in proposals:
        human.request("POST", f"/api/v1/jobs/{job_id}/decisions", {"execution_request_id": execution_id,
            "manifest_sha256": digest, "approved": True, "comment": "Disposable parallel dispatch acceptance."})
    deadline = time.monotonic() + 25
    terminal = {"succeeded", "completed", "failed", "cancelled", "timeout", "quarantined"}
    native_ids = set()
    while time.monotonic() < deadline:
        attempts = [agent.request("GET", f"/api/v1/jobs/{job_id}")["attempts"][-1] for job_id, _, _ in proposals]
        require(all(item["state"] not in terminal for item in attempts), "An earlier job finished before the queue dispatched")
        native_ids = {str(item["native_identity"]["job_id"]) for item in attempts if item.get("native_identity")}
        if len(native_ids) == 8:
            break
        time.sleep(0.25)
    else:
        raise SmokeFailure(f"Only {len(native_ids)} of eight jobs received distinct scheduler IDs before the dispatch deadline")
    interrupt_master(directory, worker)
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        attempts = [agent.request("GET", f"/api/v1/jobs/{job_id}")["attempts"][-1] for job_id, _, _ in proposals]
        require({str(item["native_identity"]["job_id"]) for item in attempts if item.get("native_identity")} == native_ids,
            "SSH recovery changed the confirmed scheduler job identities")
        require(all(item["state"] not in terminal - {"succeeded", "completed"} for item in attempts), "A parallel fixture job failed")
        if all(item["state"] in {"succeeded", "completed"} and item["accounting_state"] == "settled"
                and item["collection_state"] == "complete" for item in attempts):
            break
        time.sleep(0.5)
    else:
        raise SmokeFailure("Parallel fixture jobs did not finish accounting and output collection")
    budget = agent.request("GET", f"/api/v1/projects/{project_id}/allowance")
    require(budget["reserved"] == {"cpu_core_seconds": 0, "gpu_device_seconds": 0}, "Parallel fixture retained reservations")
    require(budget["spent"]["cpu_core_seconds"] > 0, "Parallel fixture did not charge actual usage")


def collection_recovery(human, demo, base, directory, worker, command, output, faults, agent):
    """Crash after accepted native status, then interrupt a partially published output."""
    before = agent.request("GET", f"/api/v1/projects/{demo['project_id']}/allowance")["spent"]
    block, repetitions = b"native-recovery\n", 400000
    source = ("import time\nfrom pathlib import Path\ntime.sleep(2)\nPath('result.json').write_bytes(" + repr(block) + f" * {repetitions})\n").encode()
    (faults / "arm-terminal").touch()
    job_id, digest, execution_id = create_job(agent, demo, "slurm", source, "interrupted-native-collection")
    decision = human.request("POST", f"/api/v1/jobs/{job_id}/decisions", {"execution_request_id": execution_id,
        "manifest_sha256": digest, "approved": True, "comment": "Disposable native recovery acceptance."})
    attempt_id = decision["attempt_id"]
    try:
        with PortalProbes(base, human, agent, demo["project_id"], job_id) as probes:
            deadline = time.monotonic() + 90
            while worker.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            require(worker.poll() == 73 and (faults / "lost-terminal-reply").exists(), "Native terminal reply-loss fault did not occur")
            detail = agent.request("GET", f"/api/v1/jobs/{job_id}")
            require(len(detail["attempts"]) == 1, "Reply loss created another compute attempt")
            early = detail["attempts"][0]
            require(early["state"] == "succeeded" and early["collection_state"] == "pending",
                "Native terminal status was not public before output collection")
            require(early["accounting_state"] in {"settled", "settlement_pending"}, "Native terminal accounting was lost")
            native = early["native_identity"]
            old_master = master_pid(directory)
            if old_master is not None:
                os.kill(old_master, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while (directory / "s").exists() and time.monotonic() < deadline:
                time.sleep(.05)
            require(not (directory / "s").exists(), "Crashed fixture master did not close")
            worker = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output, umask=0o002)
            try:
                deadline = time.monotonic() + 90
                while not (faults / "collection-interrupted").exists() and time.monotonic() < deadline:
                    require(worker.poll() is None, "Connector could not restart its durable native report")
                    time.sleep(.05)
                require((faults / "collection-interrupted").exists(), "Multichunk native output did not reach the interruption point")
                pending = agent.request("GET", f"/api/v1/jobs/{job_id}")["attempts"][0]
                require(pending["state"] == "succeeded" and pending["native_identity"] == native,
                    "Collection hid terminal status or changed native identity")
                interrupt_master(directory, worker)
                (faults / "resume-collection").touch()
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    detail = agent.request("GET", f"/api/v1/jobs/{job_id}")
                    require(len(detail["attempts"]) == 1, "Collection recovery created another compute attempt")
                    final = detail["attempts"][0]
                    require(final["native_identity"] == native and final["state"] == "succeeded", "Recovery changed native execution")
                    if final["accounting_state"] == "settled" and final["collection_state"] == "complete":
                        break
                    time.sleep(.25)
                else:
                    raise SmokeFailure("Native collection recovery did not finish")
                artifacts = agent.request("GET", f"/api/v1/attempts/{attempt_id}/artifacts")["artifacts"]
                require(len(artifacts) == 1 and artifacts[0]["size_bytes"] == len(block) * repetitions, "Recovered output is missing or truncated")
                actual = hashlib.sha256()
                for offset in range(0, artifacts[0]["size_bytes"], 1024 * 1024):
                    content = agent.request("GET", f"/api/v1/artifacts/{artifacts[0]['id']}/content?offset={offset}", binary=True)
                    require(len(content) == min(1024 * 1024, artifacts[0]["size_bytes"] - offset), "Recovered output range differs")
                    actual.update(content)
                require(actual.hexdigest() == artifacts[0]["sha256"] == hashlib.sha256(block * repetitions).hexdigest(),
                    "Interrupted native output failed integrity verification")
                require((faults / "submissions").read_text().splitlines().count(attempt_id) == 1, "Recovery repeated native submission")
                require(final["accounting"]["cpu_core_seconds"] > 0, "Recovery fixture did not establish nonzero native CPU usage")
                expected = {key: before[key] + final["accounting"][key] for key in before}
                for _ in range(2):
                    allowance = agent.request("GET", f"/api/v1/projects/{demo['project_id']}/allowance")
                    require(allowance["spent"] == expected and allowance["reserved"] == {key: 0 for key in before},
                        "Native recovery lost or duplicated settlement, or retained a reservation")
                    time.sleep(.25)
            except BaseException:
                worker.terminate(); worker.wait(timeout=15)
                raise
    except BaseException:
        if worker.poll() is None:
            worker.terminate(); worker.wait(timeout=15)
        raise
    print(json.dumps({"native_collection_recovery": True, "accepted_terminal_reply_lost": True,
        "native_submissions": 1, "output_bytes": len(block) * repetitions, "sha256_verified": True,
        "portal_probes": probes.count, "maximum_portal_latency_ms": round(max(probes.samples) * 1000, 1)}))
    return worker


def cluster_check(human, demo):
    """Use the same preview, review and settled results a human sees."""
    import re
    environment_path = f"/clusters/{demo['connection_id']}/environments/new/"
    page = human.request("GET", environment_path, binary=True)
    fields = ConfigurationFields(); fields.feed(page.decode())
    human.request("POST", environment_path, {"action": "inspect", "kind": "modules", "interpreter": "python3",
        "modules": "fixture-python/1.0", "module_init": "", "module_paths": "/opt/fixture/modulefiles",
        "connection_revision": fields.values["connection_revision"]}, form=True, binary=True)
    inspection_path = redirected_path(human)
    require("?task=" in inspection_path, "Environment inspection was not queued through the personal connector")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        page = human.request("GET", inspection_path, binary=True)
        require(b"Inspection failed:" not in page and b"Inspection expired." not in page, "Selected module inspection failed")
        if b"Compute-node availability is unverified" in page:
            require(b'<option value="/usr/bin/python3"' in page, "Module Python was not offered in the detected dropdown")
            break
        time.sleep(0.25)
    else:
        raise SmokeFailure("Selected module inspection did not finish")
    human.request("POST", f"/clusters/{demo['connection_id']}/environments/new/", {
        "name": "Fixture cluster modules", "kind": "modules", "interpreter": "python3",
        "modules": "fixture-python/1.0", "module_init": "", "module_paths": "/opt/fixture/modulefiles",
    }, form=True, binary=True)
    path = f"/clusters/{demo['connection_id']}/test/"
    page = human.request("GET", path, binary=True)
    fields = ClusterCheckFields();fields.feed(page.decode())
    environment = re.search(rb'<option value="([0-9a-f-]{36})">Fixture cluster modules</option>', page)
    require(environment is not None, "Module recipe was not offered for this cluster's test")
    fields.values["environment"] = environment.group(1).decode()
    require(b"Prepare test for review" in page, "Cluster check preview is unavailable")
    page = human.request("POST", path, fields.values, form=True, binary=True)
    review_path = redirected_path(human)
    require(review_path.startswith("/review/"), "Cluster check did not open execution review")
    require(b"compute-relay-cluster-check-v1" in page, "Review lacks the diagnostic source")
    fields = ConfigurationFields();fields.feed(page.decode())
    require("execution_request_id" in fields.values, "Cluster check bypassed human approval")
    human.request("POST", review_path, {**fields.values, "decision": "approve",
        "comment": "Approved only for the disposable Slurm fixture."}, form=True, binary=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        page = human.request("GET", review_path, binary=True)
        if (b'data-state="succeeded" data-accounting-state="settled"' in page
                and b'<dt>Output collection</dt><dd>complete</dd>' in page):
            break
        time.sleep(0.5)
    else:
        raise SmokeFailure("Cluster check did not finish accounting and output collection")
    page = human.request("GET", path, binary=True)
    results = PreformattedText();results.feed(page.decode())
    for block in results.blocks:
        try:
            result = json.loads(block["text"])
        except ValueError:
            continue
        if isinstance(result, dict) and result.get("marker") == "compute-relay-cluster-check-v1":
            require(result.get("hostname") and result.get("python"), "Cluster check lacks remote identity")
            require("fixture-python/1.0" in result.get("loaded_modules", "").split(":"),
                    "The cluster-provided module command did not load the selected module")
            return
    raise SmokeFailure("Cluster check returned no diagnostic JSON output")


def run():
    run.stage = "read_fixture_metadata"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-file", type=Path, default=Path("/run/demo/demo.json"))
    args = parser.parse_args()
    demo = json.loads(args.demo_file.read_bytes())
    base = "http://control:8765"
    run.stage = "human_login"
    human = Client(base);human.login(demo["owner_username"], demo["owner_password"])
    # Adopt the existing synthetic connection so its project and curated recipe
    # remain usable, while replacing its manually bootstrapped worker binding.
    run.stage = "download_setup"
    page = human.request("GET", f"/clusters/{demo['connection_id']}/", binary=True)
    import re
    revision = re.search(rb'name="setup_revision" value="([0-9]+)"', page)
    setup = human.request("POST", f"/clusters/{demo['connection_id']}/connect/",
        {"setup_revision": revision.group(1).decode()} if revision else {}, form=True)
    require("pairing_token" in setup and "no-store" in human.last_headers.get("cache-control", ""), "Setup did not return a private pairing document")
    require(setup["local"].get("automatic_reconnect_enabled", False) is False, "Automatic reconnect was enabled without owner consent")
    setup["local"] = {**setup["local"], "identity_file": "/run/fixture-ssh/id_ed25519",
        "known_hosts": "/run/fixture-ssh/known_hosts", "session_seconds": 60}
    with tempfile.TemporaryDirectory(prefix="cb-", dir="/tmp") as temp:
        root = Path(temp);root.chmod(0o700)
        path = root / "setup.json";path.write_text(json.dumps(setup));path.chmod(0o600)
        directory = root / "connectors" / "lab"
        command = [sys.executable, __file__, "--fault-child", str(root), "connector", "--state-directory", str(directory),
            "--non-interactive", "--allow-insecure-http"]
        log = root / "connector.log"
        with log.open("wb") as output:
            run.stage = "connector_start_and_discovery"
            worker = subprocess.Popen([*command, "--setup", str(path)], stdin=subprocess.DEVNULL, stdout=output, stderr=output, umask=0o002)
            try:
                deadline = time.monotonic() + 45
                agent = Client(base, token=(args.demo_file.parent / "agent-token").read_text().strip())
                while time.monotonic() < deadline:
                    require(worker.poll() is None, "Personal connector exited during pairing or discovery")
                    page = human.request("GET", f"/clusters/{demo['connection_id']}/", binary=True)
                    if (directory / "state.json").exists() and b'data-discovery-state="ready"' in page and json.loads((directory / "state.json").read_bytes()).get("defaults_initialized"):
                        break
                    time.sleep(0.25)
                else:
                    raise SmokeFailure("Personal connector identification timed out")
                require(all(p.stat().st_mode & 0o777 == 0o700 for p in [root / "connectors", directory]), "Nested state was not created privately")
                state = json.loads((directory / "state.json").read_bytes())
                require(state["connection_id"] == demo["connection_id"], "Pairing replaced the cluster identity")
                require(not agent.request("GET", "/api/v1/connections")["connections"], "Unconfirmed configuration is available to agents")
                run.stage = "post_login_directory_and_configuration"
                finish_configuration(human, demo, directory)
                reconnect_policy(human, demo, True)
                # Real submitted Python, result hashes and final Slurm accounting
                # all travel through the newly established master socket.
                run.stage = "reviewed_native_job"
                job_smoke(base, args.demo_file, 90)
                run.stage = "natural_session_expiry"
                original_master = master_pid(directory)
                require(original_master is not None, "Fixture SSH master is not running before expiry")
                wait_for_master_change(directory, worker, original_master)
                run.stage = "eight_parallel_native_submissions"
                parallel_dispatch(human, demo, base, directory, worker)
                run.stage = "portal_cluster_check"
                cluster_check(human, demo)
                run.stage = "native_collection_reply_loss_and_restart"
                worker = collection_recovery(human, demo, base, directory, worker, command, output, root, agent)
                run.stage = "owner_revokes_automatic_reconnect"
                reconnect_policy(human, demo, False)
                current_master = master_pid(directory)
                require(current_master is not None, "No fixture master to test revoked recovery")
                os.kill(current_master, signal.SIGKILL)
                worker.wait(timeout=20)
                require(worker.returncode == 0 and master_pid(directory) is None,
                    "Revoked automatic reconnection created another SSH login")
                page = human.request("GET", f"/clusters/{demo['connection_id']}/", binary=True)
                require(b"SSH sign-in required." in page, "Revoked recovery did not request a human reconnect")
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=15)
        run.stage = "shutdown_and_resume"
        require(not (directory / "s").exists(), "Connector shutdown left its authenticated master socket")
        original = json.loads((directory / "state.json").read_bytes())
        resumed = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, umask=0o002)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not (directory / "s").exists():
                require(resumed.poll() is None, "Personal connector could not resume its saved worker identity")
                time.sleep(0.1)
            require((directory / "s").exists(), "Resumed SSH master was not established")
            require(json.loads((directory / "state.json").read_bytes())["worker_id"] == original["worker_id"], "Resume registered another worker")
            run.stage = "remove_configuration_and_revoke"
            removal_path = f"/clusters/{demo['connection_id']}/remove/"
            confirmation = human.request("GET", removal_path, binary=True)
            fields = {name.decode(): value.decode() for name, value in re.findall(
                rb'name="(setup_revision|connection_revision_sha256)" value="([^"]*)"', confirmation)}
            human.request("POST", removal_path, fields, form=True, binary=True)
            resumed.wait(timeout=15)
            require(resumed.returncode == 0 and not (directory / "s").exists(),
                f"Revocation did not finish cleanly (exit={resumed.returncode}, socket_present={(directory / 's').exists()})")
            require(not agent.request("GET", "/api/v1/connections")["connections"], "Removed configuration remains executable")
        finally:
            if resumed.poll() is None:
                resumed.terminate();resumed.wait(timeout=15)
        print(json.dumps({"ok": True, "fixture_only": True, "automatic_worker_pairing": True,
            "native_ssh_master": True, "nested_private_state": True, "discovery": True, "reviewed_slurm_execution": True,
            "same_worker_on_resume": True, "master_closed_on_shutdown": True,
            "removed_configuration": True, "master_closed_on_revocation": True,
            "post_login_directory_browser": True, "created_selected_folder": True,
            "owner_configuration_persisted": True, "reviewed_cluster_check": True,
            "cluster_provided_modules": True, "selected_environment_inspection": True,
            "parallel_dispatch_jobs": 8, "parallel_accounting_settled": True,
            "abrupt_ssh_recovery": True, "same_native_ids_after_recovery": True,
            "natural_expiry_renewed": True, "revoked_reconnect_stopped": True,
            "default_reconnect_disabled": True, "interrupted_multichunk_output": True,
            "lost_early_report_reply_and_restart": True}))


if __name__ == "__main__":
    try:
        if len(sys.argv) > 2 and sys.argv[1] == "--fault-child":
            fault_connector(Path(sys.argv[2]), sys.argv[3:])
        else:
            run()
    except Exception as error:
        print(json.dumps({"ok": False, "error_type": type(error).__name__, "stage": getattr(run, "stage", "start"),
            "message": str(error) if isinstance(error, SmokeFailure) else "Fixture check failed"}), file=sys.stderr)
        raise SystemExit(1) from None
