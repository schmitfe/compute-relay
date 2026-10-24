#!/usr/bin/env python3
"""Create a private fresh-agent acceptance project; never read credential bytes."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "examples/agent-integration"
CASES = {"00": "00-smoke", "01": "01-local-science", "02": "02-hybrid", "03": "03-pipeline", "04": "04-dense-ising"}


def _private_write(path, content):
    data = content.encode() if isinstance(content, str) else content
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def _destination(value):
    destination = Path(value).absolute()
    resolved = destination.resolve()
    if resolved.is_relative_to(ROOT) and not resolved.is_relative_to(ROOT / "local"):
        raise ValueError("Generated projects inside the checkout must use ignored local/")
    roots = [Path("/tmp").resolve(), (ROOT / "local").resolve()]
    if not any(resolved != root and resolved.is_relative_to(root) for root in roots):
        raise ValueError("Choose a fresh destination below this repository's local/ or /tmp/")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination already exists; existing projects are never overwritten")
    return resolved


def _broker_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("Use a credential-free HTTPS broker origin, or HTTP on loopback")
    parsed.port  # Reject malformed ports without contacting a host.
    return value.rstrip("/")


def _absolute_reference(value, label):
    path = Path(value)
    if not path.is_absolute() or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} must be an absolute path without control characters")
    # Do not stat, resolve, open, copy, or hash the referenced token file.
    return str(path)


def create(case, destination, broker_url, project_token_file, python, extensions=""):
    if case not in CASES:
        raise ValueError("Unknown numbered case")
    destination = _destination(destination)
    selected_extensions = sorted(set(value.strip() for value in extensions.split(",") if value.strip()))
    if set(selected_extensions) - {"storage", "environments", "workflows"}:
        raise ValueError("Unknown optional extension")
    environment = {"BROKER_CONTROL_URL": _broker_url(broker_url),
                   "BROKER_PROJECT_TOKEN_FILE": _absolute_reference(project_token_file, "Project token file"),
                   "BROKER_EXTENSIONS": ",".join(selected_extensions)}
    command = _absolute_reference(python, "MCP Python interpreter")
    config = json.loads((KIT / CASES[case] / "case.json").read_text())
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.mkdir(mode=0o700)
    try:
        for folder in (".codex", "common", "results"):
            (destination / folder).mkdir(mode=0o700)
        sources = [path for path in (KIT / CASES[case]).iterdir() if path.is_file()]
        sources += [path for path in (KIT / "common").iterdir() if path.suffix == ".py"]
        sources += [KIT / "README.md", KIT / "REFERENCES.md", KIT / "VALIDATION.md", KIT / "record.schema.json"]
        hashes = {}
        for source in sources:
            relative = Path("common") / source.name if source.parent.name == "common" else Path("SETUP.md" if source.name == "README.md" else source.name)
            content = source.read_bytes()
            _private_write(destination / relative, content)
            hashes[str(relative)] = hashlib.sha256(content).hexdigest()
        args = ["-m", "compute_relay.mcp"]
        toml = "[mcp_servers.compute_relay]\ncommand = " + json.dumps(command) + "\nargs = " + json.dumps(args) + "\n\n[mcp_servers.compute_relay.env]\n"
        toml += "".join(key + " = " + json.dumps(value) + "\n" for key, value in environment.items())
        _private_write(destination / ".codex/config.toml", toml)
        _private_write(destination / ".mcp.json", json.dumps({"mcpServers": {"compute_relay": {"type": "stdio", "command": command, "args": args, "env": environment}}}, indent=2) + "\n")
        record = {"schema_version": "1.0", "case_id": case, "status": "not_run", "client": {"name": "", "version": "", "model": ""},
                  "checks": [{"id": check["id"], "status": "pending", "detail": ""} for check in config["rubric"]],
                  "executions": [], "artifacts": [], "usage": {key: None for key in ("cpu_core_seconds", "gpu_device_seconds", "reserved_cpu_core_seconds", "reserved_gpu_device_seconds", "storage_bytes", "wall_seconds")}, "notes": []}
        _private_write(destination / "record.template.json", json.dumps(record, indent=2) + "\n")
        _private_write(destination / "project.json", json.dumps({"schema_version": "1.0", "case_id": case,
                       "project_identity_source": "api_key",
                       "broker_origin": environment["BROKER_CONTROL_URL"], "generated_at": datetime.now(timezone.utc).isoformat(),
                       "source_sha256": hashes, "required_extensions": config["required_extensions"],
                       "requested_client_extensions": selected_extensions}, indent=2) + "\n")
        _private_write(destination / ".gitignore", ".codex/\n.mcp.json\nproject.json\nrecord*.json\nresults/\n*.token\npreflight.json\n")
    except BaseException:
        shutil.rmtree(destination)  # Only the new directory created by this call.
        raise
    return destination


def record(project, input_name, name):
    project = Path(project).resolve()
    if not any(project != root and project.is_relative_to(root) for root in (Path("/tmp").resolve(), (ROOT / "local").resolve())):
        raise ValueError("Recording is restricted to a generated private project")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or Path(input_name).name != input_name:
        raise ValueError("Use a simple report filename and result name")
    fd = os.open(project / input_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        content = stream.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024 or re.search(rb"cb[pw]_[A-Za-z0-9_-]{16,}|BEGIN [A-Z ]*PRIVATE KEY", content):
        raise ValueError("Report exceeds its limit or contains credential-like material")
    report = json.loads(content)
    config = json.loads((project / "case.json").read_text())
    required = {"schema_version", "case_id", "status", "client", "checks", "executions", "artifacts", "usage", "notes"}
    if not isinstance(report, dict) or set(report) != required or report["schema_version"] != "1.0" or report["case_id"] != config["id"]:
        raise ValueError("Report does not match this case's recording format")
    if report["status"] not in {"passed", "failed", "blocked"}:
        raise ValueError("Choose passed, failed, or blocked after the run")
    if (not isinstance(report["client"], dict) or set(report["client"]) != {"name", "version", "model"}
            or any(not isinstance(value, str) for value in report["client"].values())
            or (report["status"] == "passed" and not all(report["client"].values()))
            or not isinstance(report["notes"], list) or any(not isinstance(note, str) for note in report["notes"])):
        raise ValueError("Record the client/version/model and use plain-text notes")
    expected = {item["id"] for item in config["rubric"]}
    checks = report["checks"]
    if (not isinstance(checks, list) or any(not isinstance(item, dict) for item in checks) or len(checks) != len(expected) or {item.get("id") for item in checks} != expected
            or any(set(item) != {"id", "status", "detail"} or item["status"] not in {"pass", "fail", "blocked", "pending"}
                   or not isinstance(item["detail"], str) for item in checks)):
        raise ValueError("Each rubric check needs one explicit result")
    if report["status"] == "passed" and any(item["status"] != "pass" for item in checks):
        raise ValueError("A passed case requires every rubric check to pass")
    usage_keys = {"cpu_core_seconds", "gpu_device_seconds", "reserved_cpu_core_seconds", "reserved_gpu_device_seconds", "storage_bytes", "wall_seconds"}
    if not isinstance(report["usage"], dict) or set(report["usage"]) != usage_keys or any(value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value < 0 or value > 2**53 - 1)
                                                   for value in report["usage"].values()):
        raise ValueError("Usage must contain bounded nonnegative numbers or null")
    if report["status"] == "passed" and (any(value is None for value in report["usage"].values())
            or report["usage"]["reserved_cpu_core_seconds"] != 0 or report["usage"]["reserved_gpu_device_seconds"] != 0):
        raise ValueError("Passed cases require measured usage and no outstanding reservations")
    if not isinstance(report["artifacts"], list) or not isinstance(report["executions"], list):
        raise ValueError("Artifacts and executions must be lists")
    for artifact in report["artifacts"]:
        if (not isinstance(artifact, dict) or set(artifact) != {"path", "sha256", "size_bytes"} or not isinstance(artifact["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
                or Path(artifact["path"]).is_absolute() or ".." in Path(artifact["path"]).parts
                or type(artifact["size_bytes"]) is not int or artifact["size_bytes"] < 0):
            raise ValueError("Artifacts need safe relative names and measured hashes/sizes")
    if report["status"] == "passed" and len(report["executions"]) < config["minimum_executions"]:
        raise ValueError("The case lacks its required execution evidence")
    if len(report["executions"]) > config["maximum_executions"]:
        raise ValueError("Execution count exceeds this case's declared envelope")
    for execution in report["executions"]:
        if not isinstance(execution, dict) or set(execution) != {"job_id", "attempt_id", "native_job_id", "state", "accounting_state", "backend"}:
            raise ValueError("Unexpected execution fields")
        UUID(execution["job_id"])
        if execution["attempt_id"] is not None:
            UUID(execution["attempt_id"])
        if report["status"] == "passed" and (execution["backend"] != "slurm" or execution["state"] not in {"completed", "succeeded"}
                or execution["accounting_state"] != "settled" or execution["attempt_id"] is None or not execution["native_job_id"]):
            raise ValueError("Passed remote cases require settled native Slurm execution evidence")
    encoded = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
    target = project / "results" / (name + ".json")
    _private_write(target, encoded)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    new = commands.add_parser("create")
    new.add_argument("--case", choices=CASES, required=True)
    for name in ("destination", "broker-url", "project-token-file", "python"):
        new.add_argument("--" + name, required=True)
    new.add_argument("--extensions", default="", help="Operator-selected client extensions; disabled by default; grants remain server-controlled")
    saved = commands.add_parser("record")
    saved.add_argument("--project", required=True)
    saved.add_argument("--input", required=True)
    saved.add_argument("--name", required=True)
    args = parser.parse_args()
    try:
        if args.command == "list":
            print("\n".join(key + " " + value for key, value in CASES.items()))
        elif args.command == "create":
            path = create(args.case, args.destination, args.broker_url, args.project_token_file, args.python, args.extensions)
            print(f"Private acceptance project created: {path}\nStart a fresh supported agent there and use PROMPT.md.")
        else:
            print(record(args.project, args.input, args.name))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"Acceptance kit: {type(error).__name__}; inputs were rejected without displaying their content.\n")


if __name__ == "__main__":
    main()
