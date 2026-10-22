#!/usr/bin/env python3
"""Synthetic Docker-only personal connector acceptance; no institutional keys."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from container_smoke import Client, PreformattedText, SmokeFailure, require, run as job_smoke


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


def finish_configuration(human, demo, directory):
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
    chosen = home + "/personal-broker-jobs"
    original_root = json.loads((directory / "state.json").read_bytes())["connection"]["remote_root"]
    human.request("POST", f"/clusters/{connection_id}/directories/", {
        "action": "mkdir", "path": home, "name": "personal-broker-jobs",
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


def cluster_check(human, demo):
    """Use the same preview, review and settled results a human sees."""
    path = f"/clusters/{demo['connection_id']}/test/"
    page = human.request("GET", path, binary=True)
    fields = ClusterCheckFields();fields.feed(page.decode())
    require(b"Prepare test for review" in page, "Cluster check preview is unavailable")
    page = human.request("POST", path, fields.values, form=True, binary=True)
    review_path = redirected_path(human)
    require(review_path.startswith("/review/"), "Cluster check did not open execution review")
    require(b"compute-broker-cluster-check-v1" in page, "Review lacks the diagnostic source")
    fields = ConfigurationFields();fields.feed(page.decode())
    require("execution_request_id" in fields.values, "Cluster check bypassed human approval")
    human.request("POST", review_path, {**fields.values, "decision": "approve",
        "comment": "Approved only for the disposable Slurm fixture."}, form=True, binary=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        page = human.request("GET", review_path, binary=True)
        if "succeeded · accounting settled".encode() in page:
            break
        time.sleep(0.5)
    else:
        raise SmokeFailure("Cluster check did not succeed with settled accounting")
    page = human.request("GET", path, binary=True)
    results = PreformattedText();results.feed(page.decode())
    for block in results.blocks:
        try:
            result = json.loads(block["text"])
        except ValueError:
            continue
        if isinstance(result, dict) and result.get("marker") == "compute-broker-cluster-check-v1":
            require(result.get("hostname") and result.get("python"), "Cluster check lacks remote identity")
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
    setup["local"] = {"identity_file": "/run/fixture-ssh/id_ed25519", "known_hosts": "/run/fixture-ssh/known_hosts", "session_seconds": 300}
    with tempfile.TemporaryDirectory(prefix="cb-", dir="/tmp") as temp:
        root = Path(temp);root.chmod(0o700)
        path = root / "setup.json";path.write_text(json.dumps(setup));path.chmod(0o600)
        directory = root / "connectors" / "lab"
        command = [sys.executable, "-m", "compute_broker.cli", "connector", "--state-directory", str(directory),
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
                # Real submitted Python, result hashes and final Slurm accounting
                # all travel through the newly established master socket.
                run.stage = "reviewed_native_job"
                job_smoke(base, args.demo_file, 90)
                run.stage = "portal_cluster_check"
                cluster_check(human, demo)
            finally:
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
            "owner_configuration_persisted": True, "reviewed_cluster_check": True}))


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(json.dumps({"ok": False, "error_type": type(error).__name__, "stage": getattr(run, "stage", "start"),
            "message": str(error) if isinstance(error, SmokeFailure) else "Fixture check failed"}), file=sys.stderr)
        raise SystemExit(1) from None
