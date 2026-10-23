"""Explicit workspace file transfer; no model-visible bytes or cluster credentials."""

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

from jsonschema import ValidationError

from compute_broker.domain.validation import relative_path, strict_json, validate
from compute_broker.mcp.setup import (_absolute_path, _broker_url, _check_path, _ensure_directory,
    _parent_descriptor, _read_regular, _validate_key)
from compute_broker.worker.client import ControlError, JsonClient


MAX_SOURCE_BYTES = 1024 * 1024
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_RESULT_BYTES = 50 * 1024 * 1024
PRIVATE_NAMES = {".compute-broker", ".compute-broker-receipt.json", ".git", ".codex", ".claude", ".ssh", ".aws", "_broker"}


def _logical(value):
    try:
        relative_path(value)
    except ValidationError:
        raise ValueError("Choose a canonical workspace-relative file path.") from None
    if any(component in PRIVATE_NAMES for component in value.split("/")):
        raise ValueError("Credential, Git and client configuration directories cannot be transferred.")
    return value


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def _directory(parent, name, *, create=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _read_published(directory, name, maximum):
    """Read private immutable metadata, including an interrupted link publication.

    A completed atomic publication can temporarily share its inode with the
    private temporary name. Its verified bytes, not its link count, identify a
    bundle. Credentials and selected source files retain their no-hardlink rule.
    """
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > maximum):
            raise ValueError("A published receipt is not a bounded private ordinary file.")
        content = stream.read(maximum + 1)
        if len(content) > maximum or len(content) != info.st_size:
            raise ValueError("A published receipt changed during verification.")
    return content


def _write_relative(directory, path, content, *, accept_identical=False):
    parts = path.split("/")
    if len(parts) > 1:
        with _directory(directory, parts[0], create=True) as child:
            return _write_relative(child, "/".join(parts[1:]), content, accept_identical=accept_identical)
    temporary = ".compute-broker-transfer-" + uuid4().hex
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if stream.write(content) != len(content):
                raise OSError("A private transfer write was incomplete.")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            if not accept_identical:
                raise
            if _read_published(directory, path, len(content)) != content:
                raise ValueError("The saved bundle receipt failed integrity verification.") from None
    finally:
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)


class WorkspaceClient:
    def __init__(self, directory=".", *, client=None):
        self.root = _absolute_path(directory)
        _check_path(self.root, directory=True)
        private = self.root / ".compute-broker"
        _check_path(private, directory=True, private=True)
        config_bytes = _read_regular(private / "connection.json", private=True)
        if config_bytes is None:
            raise ValueError("Initialize this workspace with init-mcp first; existing setups can use --reuse-key.")
        config = strict_json(config_bytes)
        if (not isinstance(config, dict) or set(config) != {"schema_version", "broker_url", "workspace_root"}
                or config["schema_version"] != "1.0" or config["workspace_root"] != str(self.root)):
            raise ValueError("The saved connection does not belong to this workspace.")
        self.broker_url = _broker_url(config["broker_url"])
        key = _read_regular(private / "project.key", private=True)
        if key is None:
            raise ValueError("The workspace project key is missing.")
        token = _validate_key(key.decode().strip())
        self.client = client if client is not None else JsonClient(self.broker_url, token)

    def _source(self, name, maximum=MAX_SOURCE_BYTES):
        path = self.root / _logical(name)
        _check_path(path)
        with _parent_descriptor(path) as parent:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
                    raise ValueError("Select an ordinary bounded source file without links.")
                content = stream.read(maximum + 1)
                after = os.fstat(stream.fileno())
                try:
                    selected = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    raise ValueError("A source file changed during staging; retry with stable files.") from None
                expected = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink)
                if (len(content) > maximum or len(content) != before.st_size
                        or expected != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink)
                        or expected != (selected.st_dev, selected.st_ino, selected.st_size,
                            selected.st_mtime_ns, selected.st_ctime_ns, selected.st_nlink)):
                    raise ValueError("A source file changed during staging; retry with stable files.")
        return content, bool(before.st_mode & 0o111)

    def _facade(self):
        from compute_broker.mcp.facade import ProjectFacade
        return ProjectFacade.from_credential(self.client)

    def stage(self, paths):
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 100
                or any(not isinstance(path, str) for path in paths) or len(set(paths)) != len(paths)):
            raise ValueError("Choose between one and 100 distinct source files.")
        sources, total = [], 0
        for name in paths:
            content, executable = self._source(name)
            total += len(content)
            if total > MAX_BUNDLE_BYTES:
                raise ValueError("The source bundle exceeds 16 MiB.")
            sources.append(({"path": name, "sha256": _digest(content), "size_bytes": len(content),
                             "executable": executable}, content))
        facade = self._facade()
        unique = {(entry["sha256"], entry["size_bytes"]) for entry, _ in sources}
        present = self.client.request("POST", f"/api/v1/projects/{facade.project_id}/objects/lookup",
            {"objects": [{"sha256": digest, "size_bytes": size} for digest, size in sorted(unique)]})
        if (not isinstance(present, dict) or set(present) != {"present"} or not isinstance(present["present"], list)
                or len(present["present"]) > len(unique)):
            raise ValueError("The broker returned an invalid content receipt.")
        known = set()
        for entry in present["present"]:
            if (not isinstance(entry, dict) or set(entry) != {"sha256", "size_bytes"}
                    or not isinstance(entry["sha256"], str) or type(entry["size_bytes"]) is not int
                    or (entry["sha256"], entry["size_bytes"]) not in unique
                    or (entry["sha256"], entry["size_bytes"]) in known):
                raise ValueError("The broker returned an invalid content receipt.")
            known.add((entry["sha256"], entry["size_bytes"]))
        reused, uploaded = len(known), 0
        for entry, content in sources:
            identity = (entry["sha256"], entry["size_bytes"])
            if identity in known:
                continue
            receipt = self.client.request("POST", f"/api/v1/projects/{facade.project_id}/objects",
                {"path": entry["path"], "content_base64": base64.b64encode(content).decode("ascii")})
            if receipt != {field: entry[field] for field in ("path", "sha256", "size_bytes", "executable")}:
                # The server's object endpoint treats uploads as non-executable;
                # executable intent belongs to the reviewed bundle descriptor.
                if receipt != {**entry, "executable": False}:
                    raise ValueError("The uploaded source failed receipt verification.")
            known.add(identity)
            uploaded += 1
        bundle = {"schema_version": "1.0", "broker_url": self.broker_url, "project_id": facade.project_id,
                  "files": sorted([entry for entry, _ in sources], key=lambda entry: entry["path"])}
        content = _encoded(bundle)
        identity = _digest(content)
        directory = self.root / ".compute-broker" / "bundles"
        try:
            _ensure_directory(directory, [])
        except FileExistsError:
            # A simultaneous stage may have created this same private folder.
            if not directory.is_dir():
                raise
        _check_path(directory, directory=True, private=True)
        destination = directory / (identity + ".json")
        with _parent_descriptor(directory) as parent:
            os.fsync(parent)
        with _parent_descriptor(destination) as parent:
            _write_relative(parent, destination.name, content, accept_identical=True)
        return {"bundle_id": identity, "file_count": len(sources), "size_bytes": total,
                "uploaded_objects": uploaded, "reused_objects": reused,
                "receipt": str(destination.relative_to(self.root))}

    def prepare(self, bundle_id, request_path, *, idempotency_key=None):
        if not isinstance(bundle_id, str) or not re.fullmatch(r"[0-9a-f]{64}", bundle_id):
            raise ValueError("Choose a valid saved bundle identifier.")
        bundle_path = self.root / ".compute-broker/bundles" / (bundle_id + ".json")
        _check_path(bundle_path, private=True)
        try:
            with _parent_descriptor(bundle_path) as parent:
                content = _read_published(parent, bundle_path.name, MAX_SOURCE_BYTES)
        except FileNotFoundError:
            content = None
        if content is None or _digest(content) != bundle_id:
            raise ValueError("The saved bundle is missing or its checksum changed.")
        bundle = strict_json(content)
        if (not isinstance(bundle, dict) or set(bundle) != {"schema_version", "broker_url", "project_id", "files"}
                or bundle["schema_version"] != "1.0"):
            raise ValueError("Invalid saved bundle format.")
        facade = self._facade()
        if bundle.get("project_id") != facade.project_id or bundle.get("broker_url") != self.broker_url:
            raise ValueError("The bundle belongs to a different broker or project.")
        request = strict_json(self._source(request_path)[0])
        if not isinstance(request, dict) or "files" in request:
            raise ValueError("Use a job request without files; the bundle supplies those descriptors.")
        request["files"] = bundle["files"]
        validate("job-request.schema.json", facade.project_request(request))
        key = idempotency_key or "bundle-prepare:" + _digest(_encoded(facade.project_request(request)))
        return facade.prepare_job(request, idempotency_key=key)

    def fetch(self, attempt_id, output):
        attempt_id = str(UUID(attempt_id))
        output = _logical(output)
        response = self.client.request("GET", f"/api/v1/attempts/{attempt_id}/artifacts")
        entries = response.get("artifacts") if isinstance(response, dict) else None
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError("The artifact inventory exceeds its bounds.")
        selected, seen, total = [], set(), 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Invalid artifact descriptor.")
            identity, path, digest, size = str(UUID(entry.get("id", ""))), _logical(entry.get("path")), entry.get("sha256"), entry.get("size_bytes")
            if (path in seen or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or type(size) is not int or not 0 <= size <= MAX_ARTIFACT_BYTES):
                raise ValueError("Invalid or duplicate artifact descriptor.")
            seen.add(path)
            total += size
            selected.append({"id": identity, "path": path, "sha256": digest, "size_bytes": size})
        if total > MAX_RESULT_BYTES:
            raise ValueError("The result bundle exceeds 50 MiB.")
        paths = {entry["path"] for entry in selected}
        if any("/".join(path.split("/")[:index]) in paths for path in paths for index in range(1, len(path.split("/")))):
            raise ValueError("Artifact file and directory paths conflict.")
        destination = self.root / output
        _check_path(destination, directory=True)
        if destination.exists():
            raise ValueError("Choose a new result directory; existing paths are never overwritten.")
        _ensure_directory(destination.parent, [])
        with _parent_descriptor(destination) as parent:
            os.mkdir(destination.name, 0o700, dir_fd=parent)
            os.fsync(parent)
            with _directory(parent, destination.name) as folder:
                for entry in selected:
                    content, _ = self.client.get_bytes(f"/api/v1/artifacts/{entry['id']}/content", max(1, entry["size_bytes"]))
                    if len(content) != entry["size_bytes"] or _digest(content) != entry["sha256"]:
                        raise ValueError("An artifact failed checksum verification. No success receipt was written.")
                    _write_relative(folder, entry["path"], content)
                receipt = {"schema_version": "1.0", "attempt_id": attempt_id, "integrity": "verified",
                    "scientific_validation": "not_checked", "files": selected}
                _write_relative(folder, ".compute-broker-receipt.json", _encoded(receipt))
        return {"attempt_id": attempt_id, "directory": output, "integrity": "verified", "file_count": len(selected),
                "size_bytes": total, "receipt": output + "/.compute-broker-receipt.json",
                "scientific_validation": "not_checked"}


def register_commands(subparsers):
    files = subparsers.add_parser("files", help="Stage explicit workspace files or fetch checksummed results")
    actions = files.add_subparsers(dest="file_action", required=True)
    stage = actions.add_parser("stage", help="Upload missing source objects and save a reusable bundle receipt")
    stage.add_argument("paths", nargs="*")
    stage.add_argument("--manifest", help="Workspace JSON file containing a files list of relative paths")
    prepare = actions.add_parser("prepare", help="Prepare a job from a saved bundle; never execute it")
    prepare.add_argument("--bundle", required=True)
    prepare.add_argument("--request", required=True, help="Workspace job JSON without files or project_id")
    prepare.add_argument("--idempotency-key")
    fetch = actions.add_parser("fetch", help="Download retained artifacts into a new workspace directory")
    fetch.add_argument("--attempt-id", required=True)
    fetch.add_argument("--output", required=True)
    for parser in (stage, prepare, fetch):
        parser.add_argument("--directory", default=".")
        parser.set_defaults(run=main)


def main(args):
    try:
        workspace = WorkspaceClient(args.directory)
        if args.file_action == "stage":
            if args.manifest and args.paths:
                raise ValueError("Choose explicit paths or a manifest, not both.")
            paths = args.paths
            if args.manifest:
                document = strict_json(workspace._source(args.manifest)[0])
                if not isinstance(document, dict) or set(document) != {"files"}:
                    raise ValueError("A source manifest contains only its files list.")
                paths = document["files"]
            result = workspace.stage(paths)
        elif args.file_action == "prepare":
            result = workspace.prepare(args.bundle, args.request, idempotency_key=args.idempotency_key)
        else:
            result = workspace.fetch(args.attempt_id, args.output)
    except ControlError as error:
        raise SystemExit(f"Workspace transfer failed: {error.code}" + (f" (HTTP {error.status})" if error.status else "")) from None
    except Exception:
        # Validation and filesystem exceptions can include private source text
        # or paths. Keep the CLI output independent of their exception strings.
        raise SystemExit("Workspace operation failed. Check the selected files, private setup, limits and destination. A failed download can leave verified partial output; choose a new destination when retrying.") from None
    print(json.dumps(result, sort_keys=True))
