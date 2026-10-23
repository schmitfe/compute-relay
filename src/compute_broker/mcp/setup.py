"""Create a private, project-local stdio MCP connection without contacting a broker."""

import getpass
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tomllib
from urllib.parse import urlsplit
import warnings


DEFAULT_BROKER_URL = "http://127.0.0.1:8768"
KEY_PATH = Path(".compute-broker/project.key")
CONFIG_PATH = Path(".codex/config.toml")
IGNORE_PATHS = ("/.compute-broker/", "/.compute-broker/project.key", "/.codex/config.toml")
MAX_FILE_BYTES = 1024 * 1024


def register_parser(subparsers):
    parser = subparsers.add_parser("init-mcp", help="Connect this project to the broker using a private API key")
    parser.add_argument("--directory", default=".", help="Project directory (default: current directory)")
    parser.add_argument("--broker-url", default=DEFAULT_BROKER_URL, help="HTTPS broker origin, or HTTP on loopback")
    parser.add_argument("--key-file", help="Import an existing private API-key file instead of prompting")
    parser.set_defaults(run=main)
    return parser


def _broker_url(value):
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Use a credential-free HTTPS broker origin, or HTTP on loopback.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Use a broker origin with a valid hostname and port.") from None
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and not loopback)
            or port == 0):
        raise ValueError("Use a credential-free HTTPS broker origin, or HTTP on loopback.")
    return value.rstrip("/")


def _absolute_path(value):
    path = Path(value).expanduser()
    if ".." in path.parts or any(ord(char) < 32 for char in str(path)):
        raise ValueError("Choose a path without parent traversal or control characters.")
    return path.absolute()


def _check_path(path, *, directory=False, private=False):
    """Check every existing ancestor, including dangling symbolic links."""
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("Symbolic links are not allowed in setup paths.")
        is_directory = current != path or directory
        if (is_directory and not stat.S_ISDIR(info.st_mode)) or (not is_directory and not stat.S_ISREG(info.st_mode)):
            raise ValueError("A setup path has an unexpected file type.")
        if current == path and private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("Existing private broker files and directories must belong to you and exclude group/other access.")


@contextmanager
def _parent_descriptor(path):
    """Traverse with no-follow directory descriptors, also during the write itself."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parent.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _read_regular(path, *, private=False):
    _check_path(path, private=private)
    try:
        with _parent_descriptor(path) as parent:
            descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Setup files must be ordinary files owned by you, without hard links.")
        if private and stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("The imported API-key file must exclude group/other access (use chmod 600).")
        value = stream.read(MAX_FILE_BYTES + 1)
        if len(value) > MAX_FILE_BYTES:
            raise ValueError("A setup file exceeds the size limit.")
        return value


def _validate_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"cbp_[A-Za-z0-9_-]{16,200}", value):
        raise ValueError("Enter a project API key created in the broker portal, without a Bearer prefix.")
    return value


def _ensure_directory(path, created):
    if path.is_dir():
        _check_path(path, directory=True)
        return
    _ensure_directory(path.parent, created)
    with _parent_descriptor(path) as parent:
        os.mkdir(path.name, mode=0o700, dir_fd=parent)
    created.append(path)


def _write(path, original, content, created):
    """Append to checked existing files, or create new private files exclusively."""
    _check_path(path)
    flags = (os.O_WRONLY if original is None else os.O_RDWR) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= os.O_CREAT | os.O_EXCL if original is None else os.O_APPEND
    with _parent_descriptor(path) as parent:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent)
    if original is None:
        created.append(path)
    with os.fdopen(descriptor, "wb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Refusing to modify a setup file with unsafe ownership or links.")
        if original is not None and os.read(descriptor, MAX_FILE_BYTES + 1) != original:
            raise ValueError("A setup file changed during initialization; retry after reviewing it.")
        stream.write(content if original is None else content[len(original):])


def initialize(directory=".", *, broker_url=DEFAULT_BROKER_URL, api_key=None, key_file=None, python=None):
    """Create local files only. API-key bytes never enter process arguments or output."""
    project = _absolute_path(directory)
    broker_url = _broker_url(broker_url)
    _check_path(project, directory=True)
    for folder in (project / ".codex", project / ".compute-broker"):
        _check_path(folder, directory=True, private=folder.name == ".compute-broker")
    key_path, config_path, ignore_path = project / KEY_PATH, project / CONFIG_PATH, project / ".gitignore"
    for path in (key_path, config_path, ignore_path):
        _check_path(path)
    if key_path.exists():
        raise ValueError("This project already has a broker API key; existing keys are never overwritten.")
    config_before = _read_regular(config_path)
    ignore_before = _read_regular(ignore_path)
    config_text = (config_before or b"").decode("utf-8")
    try:
        existing = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError:
        raise ValueError("The existing Codex configuration is not valid TOML.") from None
    servers = existing.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("The existing Codex MCP configuration cannot be extended safely.")
    if "compute_broker" in servers:
        raise ValueError("This project already configures the compute_broker MCP server.")
    interpreter = str(_absolute_path(python or sys.executable))
    addition = "\n\n[mcp_servers.compute_broker]\ncommand = " + json.dumps(interpreter) + '\nargs = ["-m", "compute_broker.mcp"]\n\n[mcp_servers.compute_broker.env]\n'
    addition += "BROKER_CONTROL_URL = " + json.dumps(broker_url) + "\n"
    addition += "BROKER_PROJECT_TOKEN_FILE = " + json.dumps(str(key_path)) + "\n"
    config_after = (config_text + addition).encode("utf-8")
    try:
        tomllib.loads(config_after.decode("utf-8"))  # Also rejects incompatible inline tables and other TOML conflicts.
    except tomllib.TOMLDecodeError:
        raise ValueError("The existing Codex MCP configuration cannot be extended safely.") from None
    ignore_text = (ignore_before or b"").decode("utf-8")
    ignore_after = (ignore_text + ("\n" if ignore_text and not ignore_text.endswith("\n") else "")
                    + "\n# Local Compute Broker credentials and MCP configuration\n" + "\n".join(IGNORE_PATHS) + "\n").encode("utf-8")
    # Ignore rules do not protect files that Git already tracks, including deleted files.
    # A removed project directory may still contain tracked paths in its parent's
    # Git index. Inspect from an existing ancestor before creating anything.
    git_directory = project
    while not git_directory.is_dir():
        git_directory = git_directory.parent
    private_paths = [str((project / relative).relative_to(git_directory))
                     for relative in (Path(".compute-broker"), CONFIG_PATH)]
    in_repository = any((ancestor / ".git").is_file() or (ancestor / ".git/HEAD").is_file()
                        for ancestor in (git_directory, *git_directory.parents))
    try:
        tracked = subprocess.run(["git", "--literal-pathspecs", "-C", str(git_directory), "ls-files", "-z", "--", *private_paths],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        if in_repository:
            raise ValueError("Install Git so initialization can check the existing repository's private paths.") from None
    else:
        if tracked.returncode != 0 and in_repository:
            raise ValueError("Git could not inspect the existing repository. Check its access before initializing MCP.")
        if tracked.returncode == 0 and tracked.stdout:
            raise ValueError("Git already tracks a broker-private path. Untrack it before initializing MCP.")
    if key_file is not None and api_key is not None:
        raise ValueError("Choose a private key file or an interactive API key, not both.")
    if key_file is not None:
        imported = _read_regular(_absolute_path(key_file), private=True)
        if imported is None:
            raise ValueError("The API-key import file does not exist.")
        api_key = imported.decode("utf-8").strip()
    if api_key is None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            api_key = getpass.getpass("Project API key (hidden; created in the broker portal): ").strip()
    api_key = _validate_key(api_key)
    created = []
    try:
        _ensure_directory(project, created)
        _ensure_directory(config_path.parent, created)
        _ensure_directory(key_path.parent, created)
        # Install exclusions before a credential can appear in the project directory.
        _write(ignore_path, ignore_before, ignore_after, created)
        _write(key_path, None, (api_key + "\n").encode("utf-8"), created)
        _write(config_path, config_before, config_after, created)
    except BaseException:
        for path in reversed(created):
            try:
                path.rmdir() if path.is_dir() else path.unlink()
            except OSError:
                pass
        raise
    return project


def main(args):
    try:
        project = initialize(args.directory, broker_url=args.broker_url, key_file=args.key_file)
    except (UnicodeError, OSError, getpass.GetPassWarning, EOFError):
        # These exceptions can contain paths or TOML source; never print imported contents.
        raise SystemExit("MCP setup could not finish. Check the broker URL, private key-file permissions, existing configuration and path safety. Use an interactive terminal to paste the key.") from None
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:
        raise SystemExit("MCP setup cancelled.") from None
    print(f"Compute Broker MCP connection created in {project}")
    print("The API key is private and Git exclusions are installed. Start a new trusted Codex chat in this project.")
