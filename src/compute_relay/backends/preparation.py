"""Deterministic, side-effect-free Slurm package preparation.

Nothing in this module imports or executes submitted source. Canonical serialization is
JCS-compatible only for the restricted ASCII-key, safe-integer manifest domain.
"""

from __future__ import annotations

import copy
import hashlib
import re
import shlex
from typing import Mapping
from uuid import UUID

import rfc8785

from .environments import EnvironmentProfileError, render_environment
from compute_relay.domain.allocation import allocated_cpus
from compute_relay.transfer import FileReference, MAX_FILE_BYTES, MAX_BUNDLE_BYTES
from compute_relay.remote.bundles import bundle_source
from compute_relay.remote.protocol import (
    JOB_PROTOCOL, JOB_OPERATIONS, MANAGEMENT_OPERATIONS, management_protocol_supported,
)


class BackendError(ValueError):
    """A package or backend configuration cannot safely be used."""


_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$")
_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,239}$")
_SAFE_INT = 2**53 - 1


def canonical_json_bytes(value: object) -> bytes:
    def check(item: object) -> None:
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, int):
            if abs(item) > _SAFE_INT:
                raise BackendError("Integer outside canonical manifest range")
            return
        if isinstance(item, str):
            try:
                item.encode("utf-8", "strict")
            except UnicodeError as exc:
                raise BackendError("Invalid Unicode in manifest") from exc
            return
        if isinstance(item, list):
            for child in item:
                check(child)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key.isascii():
                    raise BackendError("Manifest keys must be ASCII strings")
                check(child)
            return
        raise BackendError("Manifest supports only JSON objects, arrays, strings and integers")

    check(value)
    return rfc8785.dumps(value)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def valid_digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise BackendError("Expected SHA-256 digest")
    return value


def relative_path(value: object, *, allow_broker: bool = False) -> str:
    if not isinstance(value, str) or not _PATH.fullmatch(value):
        raise BackendError("Invalid relative package path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts) or value.endswith("/"):
        raise BackendError("Package paths must have canonical components")
    if not allow_broker and parts[0] in ("_broker", "stdout.log", "stderr.log"):
        raise BackendError("Broker script and log namespaces are reserved")
    return value


def positive_int(value: object, label: str, *, minimum: int = 1, maximum: int = _SAFE_INT) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise BackendError(f"Invalid {label}")
    return value


def connection_revision_sha256(connection: dict) -> str:
    if "revision_sha256" in connection:
        return valid_digest(connection["revision_sha256"])
    # Tests/local callers may omit a precomputed revision. Protected deployments
    # explicitly bind their immutable public and private configuration revisions.
    return sha256(canonical_json_bytes(connection))


def helper_source(operation: str = "submit") -> str:
    if operation == "availability":
        return bundle_source("availability")
    if operation == "discover":
        return bundle_source("discovery")
    if operation in JOB_OPERATIONS:
        return bundle_source("jobs")
    raise BackendError("Unsupported fixed helper operation")


def submission_helper_is_current(manifest: dict) -> bool:
    return (management_protocol_supported(manifest)
            and manifest.get("helper_sha256") == sha256(helper_source().encode()))


def _name(value: object) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise BackendError("Invalid scheduler name")
    return value


def _file_entry(path: str, content: bytes, executable: bool = False) -> dict:
    return {"path": path, "sha256": sha256(content), "size_bytes": len(content), "executable": executable}


def prepare(request: dict, environment: dict, connection: dict, job_revision_id: str) -> dict:
    request = copy.deepcopy(request)
    try:
        for value in (job_revision_id, request["project_id"], request["connection_id"]):
            UUID(value)
        if request["schema_version"] != "1.0":
            raise BackendError("Unsupported job schema")
        _name(request["name"])
        resources = request["resources"]
        if resources["nodes"] != 1 or type(resources["nodes"]) is not int:
            raise BackendError("Only single-node jobs are supported")
        cpus = positive_int(resources["cpus"], "CPU count", maximum=1024)
        gpus = positive_int(resources["gpus"], "GPU count", minimum=0, maximum=16)
        memory = positive_int(resources["memory_mib"], "memory", minimum=128, maximum=16777216)
        duration = positive_int(resources["time_limit_seconds"], "walltime", minimum=60, maximum=604800)
        if duration % 60:
            raise BackendError("Walltime must be a whole number of minutes")
        if gpus and "gpu_type" in resources:
            _name(resources["gpu_type"])
        elif not gpus and "gpu_type" in resources:
            raise BackendError("A zero-GPU request cannot select a GPU type")
        profile = connection.get("site_profile", {})
        if connection.get("backend") == "fake":
            profile = {"partitions": ["debug", "cpu", "gpu"], "accounts": ["broker", "research"],
                       "qos": [], **profile}
        try:
            max_cpus = allocated_cpus(cpus, profile)
        except ValueError as exc:
            raise BackendError(str(exc)) from None
        positive_int(max_cpus, "rounded CPU allocation", maximum=1024)
        max_gpus = max(gpus, positive_int(profile.get("allocated_gpus", gpus), "allocated GPU count", minimum=0, maximum=16))
        max_memory = positive_int(profile.get("allocated_memory_mib", memory), "allocated memory", maximum=16777216)
        if max_memory != memory:
            raise BackendError("Memory allocation rounding is not supported by the version-one manifest")
        # Reserve configured walltime. Final Slurm accounting may exceed this
        # estimate; settle actual usage and block subsequent overspending.
        for label, actual, config_key in (
            ("CPU count", max_cpus, "maximum_cpus"), ("GPU count", max_gpus, "maximum_gpus"),
            ("memory", max_memory, "maximum_memory_mib"), ("walltime", duration, "maximum_time_limit_seconds"),
        ):
            if config_key in profile and actual > positive_int(profile[config_key], config_key, minimum=0):
                raise BackendError(f"Effective {label} exceeds site limit")
        slurm = request.get("slurm", {})
        if set(slurm) - {"partition", "account", "qos"}:
            raise BackendError("Unrecognized scheduler option")
        if not slurm.get("partition"):
            raise BackendError("An explicit partition is required")
        for key, allowed_key in (("partition", "partitions"), ("account", "accounts"), ("qos", "qos")):
            if profile.get(allowed_key) and key not in slurm:
                raise BackendError(f"An explicit scheduler {key} is required by this connection")
            if key in slurm:
                if _name(slurm[key]) not in profile.get(allowed_key, []):
                    raise BackendError(f"Scheduler {key} is not permitted by this connection")
        command = request["command"]
        if set(command) != {"interpreter", "script", "arguments"}:
            raise BackendError("Invalid command specification")
        interpreter = command["interpreter"]
        if interpreter not in ("python3", "bash"):
            raise BackendError("Unsupported interpreter")
        script = relative_path(command["script"])
        arguments = command["arguments"]
        if not isinstance(arguments, list) or len(arguments) > 64 or any(
            not isinstance(arg, str) or len(arg) > 4096 or "\x00" in arg for arg in arguments
        ):
            raise BackendError("Invalid script arguments")
        entries = request["files"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise BackendError("Invalid input file count")
        paths: set[str] = set()
        total = 0
        for entry in entries:
            if set(entry) != {"path", "sha256", "size_bytes", "executable"}:
                raise BackendError("Invalid file metadata")
            path = relative_path(entry["path"])
            if path in paths or any(path.startswith(p + "/") or p.startswith(path + "/") for p in paths):
                raise BackendError("Conflicting package paths")
            paths.add(path)
            valid_digest(entry["sha256"])
            total += positive_int(entry["size_bytes"], "file size", minimum=0, maximum=MAX_FILE_BYTES)
            if type(entry["executable"]) is not bool:
                raise BackendError("Invalid file executable flag")
        if total > MAX_BUNDLE_BYTES or script not in paths:
            raise BackendError("Package is too large or entrypoint is missing")
        if next(entry["size_bytes"] for entry in entries if entry["path"] == script) > 1024 * 1024:
            raise BackendError("The reviewed entrypoint must fit within 1 MiB")
        outputs = request["outputs"]
        if not isinstance(outputs, list) or len(outputs) > 100 or len(outputs) != len(set(outputs)):
            raise BackendError("Invalid output list")
        for output in outputs:
            relative_path(output)
        if environment.get("id") != request["environment"]["id"] or environment.get("revision_sha256") != request["environment"]["revision_sha256"]:
            raise BackendError("Environment revision does not match request")
        try:
            wrapper, execution_prefix = render_environment(environment, interpreter)
        except EnvironmentProfileError as exc:
            raise BackendError(str(exc)) from exc
        module_environment = environment.get("kind") == "modules" or bool(environment.get("modules"))
        lines = ["#!/bin/bash -l" if module_environment else "#!/bin/bash", f"#SBATCH --job-name={request['name']}", "#SBATCH --nodes=1", "#SBATCH --ntasks=1",
                 f"#SBATCH --cpus-per-task={cpus}", f"#SBATCH --mem={memory}M", f"#SBATCH --time={duration // 60}",
                 "#SBATCH --no-requeue", "#SBATCH --output=stdout.log", "#SBATCH --error=stderr.log"]
        for key, value in slurm.items():
            lines.append(f"#SBATCH --{key}={value}")
        if gpus:
            gpu_type = resources.get("gpu_type")
            lines.append(f"#SBATCH --gres=gpu:{gpu_type + ':' if gpu_type else ''}{gpus}")
        lines += ["", "# The helper sets cwd to this immutable attempt's staged directory.",
                  "# It adds only --comment=broker-attempt:<attempt UUID> for reconciliation.",
                  "set -euo pipefail"]
        if module_environment:
            lines += ["# Use normal cluster login setup for its module command; restore cwd after site/user startup.",
                      'cd -- "${SLURM_SUBMIT_DIR:?Broker submission directory is unavailable}"']
        lines += ["export PYTHONUNBUFFERED=1", ". ./_broker/environment.sh",
                  "exec " + shlex.join([*execution_prefix, "./" + script, *arguments]), ""]
        generated = {"_broker/batch.sh": "\n".join(lines), "_broker/environment.sh": wrapper}
        manifest = {"schema_version": "1.0", "job_revision_id": job_revision_id, "backend": "slurm",
                    "adapter_version": "slurm-ssh/v1", "helper_protocol": JOB_PROTOCOL,
                    "helper_sha256": sha256(helper_source().encode()),
                    "profile_revision": positive_int(connection.get("profile_revision", 1), "profile revision"),
                    "connection_revision_sha256": connection_revision_sha256(connection), "request": request,
                    "files": sorted([*entries, *(_file_entry(path, text.encode(), path.endswith("batch.sh")) for path, text in generated.items())], key=lambda e: e["path"]),
                    "batch_script": "_broker/batch.sh", "environment_wrapper": "_broker/environment.sh",
                    "maximum_allocation": {"cpu_cores": max_cpus, "gpu_devices": max_gpus, "time_limit_seconds": duration},
                    "reservation": {"cpu_core_seconds": max_cpus * duration, "gpu_device_seconds": max_gpus * duration}}
        return {"manifest": manifest, "manifest_sha256": sha256(canonical_json_bytes(manifest)), "generated_files": generated}
    except (KeyError, TypeError, AttributeError) as exc:
        raise BackendError("Malformed preparation request or profile") from exc


def verify_package(manifest: dict, manifest_sha256: str, files: Mapping[str, bytes | FileReference], *, operation: str = "submit") -> None:
    if sha256(canonical_json_bytes(manifest)) != valid_digest(manifest_sha256):
        raise BackendError("Prepared manifest digest mismatch")
    if operation not in JOB_OPERATIONS or not management_protocol_supported(manifest):
        raise BackendError("Unsupported prepared job management protocol or operation")
    if operation == "submit" and not submission_helper_is_current(manifest):
        raise BackendError("Prepared helper revision is not installed")
    entries = manifest.get("files", [])
    paths: set[str] = set()
    for entry in entries:
        path = relative_path(entry["path"], allow_broker=True)
        if path in paths:
            raise BackendError("Duplicate manifest path")
        paths.add(path)
        # Management authenticates the sealed manifest and remote journal. It
        # must remain possible after local input bytes are lost or corrupted.
        if operation != "submit" and not files:
            continue
        content = files.get(path)
        if isinstance(content, FileReference):
            if (content.size_bytes, content.sha256) != (entry["size_bytes"], entry["sha256"]):
                raise BackendError("Transferred input descriptor differs from prepared manifest")
            continue
        if not isinstance(content, bytes) or len(content) != entry["size_bytes"] or sha256(content) != entry["sha256"]:
            raise BackendError("Staged file does not match prepared manifest")
    if not paths or ((files or operation == "submit") and paths != set(files)):
        raise BackendError("Package file set differs from prepared manifest")
