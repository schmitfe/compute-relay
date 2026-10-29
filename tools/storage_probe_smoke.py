#!/usr/bin/env python3
"""Opt-in metadata-only storage acceptance on the disposable Docker fixture.

This client writes only broker configuration through human forms. The protected
worker opens the fixture staging directory and reads filesystem metadata. No
job, remote scan, retained-usage scan, file transfer or deletion is requested.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
from uuid import UUID, uuid4

from container_smoke import Client, SmokeFailure, require, revealed_key


def validate_demo(demo):
    target = demo.get("cluster_form", {})
    require(demo.get("backend") == "slurm" and target.get("host") == "slurm-fixture"
            and target.get("user") == "brokerjob" and target.get("port") == 22
            and target.get("remote_root") == "/home/brokerjob/jobs",
            "Storage acceptance requires the explicit disposable Slurm fixture metadata")
    for field in ("connection_id", "environment_id"):
        require(str(UUID(demo[field])) == demo[field], "Fixture metadata contains a noncanonical identifier")


class ParagraphText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paragraphs, self.active = [], None
        self.suppressed = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.suppressed = True
        if tag == "p":
            self.active = []

    def handle_data(self, text):
        if self.active is not None and not self.suppressed:
            self.active.append(text)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.suppressed = False
        if tag == "p" and self.active is not None:
            self.paragraphs.append(" ".join("".join(self.active).split()))
            self.active = None


def validate_cluster_page(page):
    # Discovery status is rendered separately and may update independently.
    # Require the exact backend/user/host identity, never a substring mention.
    parsed = ParagraphText()
    parsed.feed(page.decode("utf-8"))
    require("slurm · brokerjob@slurm-fixture" in parsed.paragraphs,
            "Selected connection is not the disposable SSH fixture")


class SelectOptions(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.options, self.select, self.option = {}, None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select":
            self.select = attrs.get("name")
        elif tag == "option" and self.select:
            self.option = {"value": attrs.get("value", ""), "text": ""}

    def handle_data(self, text):
        if self.option is not None:
            self.option["text"] += text

    def handle_endtag(self, tag):
        if tag == "option" and self.option is not None:
            self.options.setdefault(self.select, []).append(self.option)
            self.option = None
        elif tag == "select":
            self.select = None


def options(page, name):
    parsed = SelectOptions()
    parsed.feed(page.decode("utf-8"))
    return [item for item in parsed.options.get(name, []) if item["value"]]


def fresh_observation(snapshot, pool_id, started):
    pool = next((item for item in snapshot.get("pools", []) if item["id"] == pool_id), None)
    require(pool is not None and pool.get("probe_supported") is True and pool.get("probe_enabled") is True,
            "Storage worker capability or explicit probe opt-in is missing")
    data = pool.get("observation", {})
    if not data or data.get("status") != "available":
        return None
    observed = datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00"))
    require(observed.tzinfo is not None, "Capacity observation lacks a timezone")
    if observed < started:
        return None
    require(data.get("source") == "statvfs" and data.get("quota", {}).get("status") == "unknown",
            "Fixed filesystem probe did not retain unknown native quota")
    for field in ("total_bytes", "available_bytes", "total_inodes", "available_inodes"):
        require(type(data.get(field)) is int and data[field] > 0, "Fixture did not return positive byte and inode counts")
    require(data["available_bytes"] <= data["total_bytes"] and data["available_inodes"] <= data["total_inodes"],
            "Fixture capacity counters are inconsistent")
    require(data.get("identity_verified") is False, "Unpinned staging compatibility root was reported verified")
    require(datetime.now(timezone.utc) - observed <= timedelta(seconds=120), "Fixture observation is stale")
    return data


def run(base_url, demo_file, timeout):
    demo = json.loads(demo_file.read_bytes())
    validate_demo(demo)
    human = Client(base_url)
    human.login(demo["owner_username"], demo["owner_password"])
    validate_cluster_page(human.request("GET", f"/clusters/{demo['connection_id']}/", binary=True))
    suffix = uuid4().hex[:12]
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    human.request("POST", "/projects/new/", {"name": "Disposable storage acceptance " + suffix,
        "mode": "approval_required", "cpu_hours": "1", "gpu_hours": "0", "max_cpus": "1", "max_gpus": "0",
        "memory_mib": "128", "wall_minutes": "1", "charge_minutes": "2", "max_active_jobs": "1", "max_total_jobs": "1",
        "expires_at": expiry, "connections": [demo["connection_id"]], "environments": [demo["environment_id"]],
        "allowed_gpu_types": []}, form=True, binary=True)
    match = re.fullmatch(r"/projects/([0-9a-f-]{36})/", urllib.parse.urlsplit(human.last_url).path)
    require(match is not None, "Disposable storage project creation failed")
    project_id = str(UUID(match.group(1)))
    html = human.request("POST", f"/projects/{project_id}/keys/new/", {"name": "Disposable read-only planner",
        "expires_at": expiry, "scopes": ["jobs:read"], "cluster_access": "inherit", "allowed_connections": [],
        "execution_mode": "approval_required", "submission_id": str(uuid4())}, form=True, binary=True)
    secret, _key_id = revealed_key(human, html)
    agent = Client(base_url, token=secret)
    require("storage" in agent.request("GET", "/api/v1/capabilities").get("enabled_extensions", []),
            "Storage extension is not enabled on control")

    settings_path = f"/projects/{project_id}/storage/"
    target_path = settings_path + "targets/new/"
    page = human.request("GET", target_path + "?connection=" + demo["connection_id"], binary=True)
    roots = options(page, "root_alias")
    require(len(roots) == 1 and roots[0]["text"].strip() == "staging-root-0 — /home/brokerjob/jobs",
            "Fixture does not advertise the exact expected protected storage root")
    human.request("POST", target_path, {"name": "Disposable metadata target " + suffix,
        "connection": demo["connection_id"], "root_alias": roots[0]["value"], "pool": ""}, form=True, binary=True)
    require(urllib.parse.urlsplit(human.last_url).path == settings_path, "Project storage target enrollment failed")
    page = human.request("GET", settings_path + "?connection=" + demo["connection_id"], binary=True)
    targets = options(page, "results")
    require(len(targets) == 1, "Expected one current fixture result-location choice")
    target_id = str(UUID(targets[0]["value"]))
    human.request("POST", settings_path, {"connection_id": demo["connection_id"], "budget_gib": "1",
        "inodes_budget": "1000", "retained_gib_estimate": "0", "retained_inodes_estimate": "0", "results": target_id},
        form=True, binary=True)
    inventory_path = f"/api/v1/projects/{project_id}/storage"
    snapshot = agent.request("GET", inventory_path)
    selected = next((item for item in snapshot["targets"] if item["id"] == target_id), None)
    require(snapshot.get("project_id") == project_id and selected is not None
            and selected["connection_id"] == demo["connection_id"], "Approved project target selection was not saved")
    pool_id = selected["pool_id"]
    toggle = settings_path + f"pools/{pool_id}/probes/"
    started = datetime.now(timezone.utc)
    human.request("POST", toggle, {"enabled": "false"}, form=True, binary=True)
    human.request("POST", toggle, {"enabled": "true"}, form=True, binary=True)
    try:
        deadline = time.monotonic() + timeout
        observation = None
        while time.monotonic() < deadline:
            snapshot = agent.request("GET", inventory_path)
            observation = fresh_observation(snapshot, pool_id, started)
            if observation:
                break
            time.sleep(1)
        require(observation is not None, "Protected worker did not return fresh fixture metadata before the deadline")
        require("/home/brokerjob/jobs" not in json.dumps(snapshot), "Agent storage inventory disclosed an absolute root")
        plan = agent.request("POST", inventory_path + "/plan", {"schema_version": "1.0", "connection_id": demo["connection_id"],
            "estimates": [{"role": "results", "target_id": target_id, "peak_bytes": 1048576, "peak_inodes": 10,
                           "retained_bytes": 1048576, "retained_inodes": 10}]})
        require(plan.get("dry_run") is True and plan.get("advisory_only") is True
                and plan.get("creates_reservations") is False and plan.get("enforced_at_dispatch") is False,
                "Storage planning lost its advisory-only contract")
        allowance = agent.request("GET", f"/api/v1/projects/{project_id}/allowance")
        require(allowance["jobs"]["admitted"] == 0 and allowance["spent"] == {"cpu_core_seconds": 0, "gpu_device_seconds": 0}
                and allowance["reserved"] == {"cpu_core_seconds": 0, "gpu_device_seconds": 0},
                "Metadata-only acceptance unexpectedly consumed compute admission or accounting")
        result = {"ok": True, "simulated": False, "project_id": project_id, "target_id": target_id,
            "observed_at": observation["observed_at"], "available_bytes": observation["available_bytes"],
            "available_inodes": observation["available_inodes"], "native_quota": "unknown",
            "identity_verified": False, "admitted_jobs": 0, "planning_status": plan["status"]}
    finally:
        human.request("POST", toggle, {"enabled": "false"}, form=True, binary=True)
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--demo-file", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--confirm-disposable-fixture", action="store_true")
    args = parser.parse_args()
    if not args.confirm_disposable_fixture:
        parser.error("Supply --confirm-disposable-fixture; this enables temporary SSH metadata checks on the disposable fixture")
    if not 1 <= args.timeout <= 120:
        parser.error("timeout must be from 1 through 120 seconds")
    try:
        run(args.base_url, args.demo_file, args.timeout)
    except SmokeFailure as error:
        print("Storage smoke failed: " + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as error:
        print(f"Storage smoke failed: client/protocol error ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
