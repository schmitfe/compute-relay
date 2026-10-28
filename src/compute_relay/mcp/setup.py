"""Create a private, project-local stdio MCP connection without contacting a broker."""

import getpass
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tomllib
from uuid import uuid4
import warnings

from compute_relay.local_config import broker_url as validate_broker_url, validate_key
from compute_relay.local_filesystem import (MAX_FILE_BYTES, absolute_path, check_path, ensure_directory,
    parent_descriptor, read_regular)


DEFAULT_BROKER_URL = "http://127.0.0.1:8768"
KEY_PATH = Path(".compute-relay/project.key")
CONFIG_PATH = Path(".codex/config.toml")
CLAUDE_CONFIG_PATH = Path(".mcp.json")
CONNECTION_PATH = Path(".compute-relay/connection.json")
IGNORE_PATHS = ("/.compute-relay/", "/.compute-relay/project.key", "/.codex/config.toml")


def register_parser(subparsers):
    parser = subparsers.add_parser("init-mcp", help="Connect this project to the broker using a private API key")
    parser.add_argument("--directory", default=".", help="Project directory (default: current directory)")
    parser.add_argument("--broker-url", default=DEFAULT_BROKER_URL, help="HTTPS broker origin, or HTTP on loopback")
    parser.add_argument("--key-file", help="Import an existing private API-key file instead of prompting")
    parser.add_argument("--client", choices=("codex", "claude", "both"), default="codex",
                        help="Project MCP configuration to create (default: codex; claude means Claude Code)")
    parser.add_argument("--with-workflow", action="store_true", help="Install the shared compute skill and append project guidance")
    parser.add_argument("--reuse-key", action="store_true", help="Reuse this project's existing private key when adding a client or workflow")
    parser.set_defaults(run=main)
    return parser


def _write(path, original, content, created):
    """Checked atomic replacement, or exclusive creation of a new private file."""
    check_path(path)
    if original is not None:
        with parent_descriptor(path) as parent:
            temporary = ".compute-relay-init-" + uuid4().hex
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if read_regular(path) != original:
                    raise ValueError("A setup file changed during initialization; retry after reviewing it.")
                mode = stat.S_IMODE(os.stat(path.name, dir_fd=parent, follow_symlinks=False).st_mode) & 0o777
                os.chmod(temporary, mode, dir_fd=parent, follow_symlinks=False)
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with parent_descriptor(path) as parent:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent)
    created.append(path)
    with os.fdopen(descriptor, "wb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Refusing to modify a setup file with unsafe ownership or links.")
        stream.write(content)


def _json_document(content, description):
    def pairs(items):
        document = {}
        for key, value in items:
            if key in document:
                raise ValueError("Duplicate JSON key")
            document[key] = value
        return document
    def invalid_number(_value):
        raise ValueError("Invalid JSON number")
    try:
        result = json.loads(content.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_number)
        if not isinstance(result, dict):
            raise ValueError("Expected JSON object")
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError(f"The existing {description} is not a valid JSON object.") from None


def _configuration(client, before, server, reuse_key):
    text = (before or b"").decode("utf-8")
    if client == "codex":
        try:
            existing = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            raise ValueError("The existing Codex configuration is not valid TOML.") from None
        servers = existing.get("mcp_servers", {})
    else:
        existing = _json_document(before, "Claude Code configuration") if before is not None else {}
        servers = existing.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("The existing MCP configuration cannot be extended safely.")
    if "compute_relay" in servers:
        if reuse_key and servers["compute_relay"] == server:
            return before
        raise ValueError("This project already configures the compute_relay MCP server; only an identical connection can be retained with --reuse-key.")
    if client == "claude":
        existing["mcpServers"] = {**servers, "compute_relay": server}
        return (json.dumps(existing, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode("utf-8")
    addition = "\n\n[mcp_servers.compute_relay]\ncommand = " + json.dumps(server["command"]) + '\nargs = ["-m", "compute_relay.mcp"]\n\n[mcp_servers.compute_relay.env]\n'
    addition += "BROKER_CONTROL_URL = " + json.dumps(server["env"]["BROKER_CONTROL_URL"]) + "\n"
    addition += "BROKER_PROJECT_TOKEN_FILE = " + json.dumps(server["env"]["BROKER_PROJECT_TOKEN_FILE"]) + "\n"
    after = (text + addition).encode("utf-8")
    try:
        tomllib.loads(after.decode("utf-8"))
    except tomllib.TOMLDecodeError:
        raise ValueError("The existing Codex MCP configuration cannot be extended safely.") from None
    return after


def _workflow_instructions(before, client):
    from .workflow import BLOCK_END, BLOCK_START, instruction_block
    text = (before or b"").decode("utf-8")
    block = instruction_block(client)
    if BLOCK_START in text or BLOCK_END in text:
        if (text.count(BLOCK_START) != 1 or text.count(BLOCK_END) != 1
                or text.find(BLOCK_START) > text.find(BLOCK_END)):
            raise ValueError("The existing managed compute workflow instructions differ; review them before initializing.")
        start, end = text.find(BLOCK_START), text.find(BLOCK_END) + len(BLOCK_END)
        current = text[start:end]
        if current == block.rstrip("\n"):
            return before
        raise ValueError("The existing managed compute workflow instructions differ; review them before initializing.")
    return (text + ("\n\n" if text else "") + block).encode("utf-8")


def _check_tracked(project, relative_paths):
    """Check even removed paths in a parent repository before creating files."""
    git_directory = project
    while not git_directory.is_dir():
        git_directory = git_directory.parent
    paths = [str((project / relative).relative_to(git_directory)) for relative in relative_paths]
    in_repository = any((ancestor / ".git").is_file() or (ancestor / ".git/HEAD").is_file()
                        for ancestor in (git_directory, *git_directory.parents))
    try:
        tracked = subprocess.run(["git", "--literal-pathspecs", "-C", str(git_directory), "ls-files", "-z", "--", *paths],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        if in_repository:
            raise ValueError("Install Git so initialization can check the existing repository's private paths.") from None
    else:
        if tracked.returncode != 0 and in_repository:
            raise ValueError("Git could not inspect the existing repository. Check its access before initializing MCP.")
        if tracked.returncode == 0 and tracked.stdout:
            raise ValueError("Git already tracks a broker-private or skill destination path. Untrack it before initializing MCP.")


def initialize(directory=".", *, broker_url=DEFAULT_BROKER_URL, api_key=None, key_file=None, python=None,
               client="codex", with_workflow=False, reuse_key=False):
    """Offline project setup. Reusing an existing key always requires explicit opt-in."""
    if client not in {"codex", "claude", "both"}:
        raise ValueError("Choose codex, claude or both for the project client.")
    clients = ("codex", "claude") if client == "both" else (client,)
    project = absolute_path(directory)
    broker_url = validate_broker_url(broker_url)
    check_path(project, directory=True)
    check_path(project / ".compute-relay", directory=True, private=True)
    key_path, ignore_path = project / KEY_PATH, project / ".gitignore"
    for path in (key_path, ignore_path, project / CONNECTION_PATH):
        check_path(path)
    if key_path.exists() and not reuse_key:
        raise ValueError("This project already has a broker API key; existing keys are never overwritten.")
    interpreter = str(absolute_path(python or sys.executable))
    server = {"command": interpreter, "args": ["-m", "compute_relay.mcp"],
              "env": {"BROKER_CONTROL_URL": broker_url, "BROKER_PROJECT_TOKEN_FILE": str(key_path)}}
    changes = []
    private_paths = [Path(".compute-relay")]
    for selected in clients:
        relative = CONFIG_PATH if selected == "codex" else CLAUDE_CONFIG_PATH
        path = project / relative
        check_path(path)
        before = read_regular(path)
        after = _configuration(selected, before, server if selected == "codex" else {"type": "stdio", **server}, reuse_key)
        changes.append((path, before, after))
        private_paths.append(relative)
    if reuse_key:
        # Both clients share one credential. Changing the selected client must
        # never send that key to another origin configured for the same file.
        for selected, relative in (("codex", CONFIG_PATH), ("claude", CLAUDE_CONFIG_PATH)):
            before = read_regular(project / relative)
            if before is None:
                continue
            if selected == "codex":
                try:
                    prior = tomllib.loads(before.decode("utf-8"))
                except tomllib.TOMLDecodeError:
                    raise ValueError("The existing Codex configuration is not valid TOML.") from None
                servers = prior.get("mcp_servers", {})
            else:
                servers = _json_document(before, "Claude Code configuration").get("mcpServers", {})
            entry = servers.get("compute_relay", {}) if isinstance(servers, dict) else {}
            prior_env = entry.get("env", {}) if isinstance(entry, dict) else {}
            if (isinstance(prior_env, dict) and prior_env.get("BROKER_PROJECT_TOKEN_FILE") == str(key_path)
                    and prior_env.get("BROKER_CONTROL_URL") != broker_url):
                raise ValueError("The existing project's API key is configured for a different broker origin. Reuse it only with that broker URL.")
    metadata = {"schema_version": "1.0", "broker_url": broker_url, "workspace_root": str(project)}
    metadata_before = read_regular(project / CONNECTION_PATH, private=True)
    if metadata_before is not None and _json_document(metadata_before, "broker workspace metadata") != metadata:
        raise ValueError("The existing broker workspace metadata belongs to another location or broker; review it before initializing.")
    changes.append((project / CONNECTION_PATH, metadata_before, metadata_before or (json.dumps(metadata, indent=2) + "\n").encode()))
    from .workflow import USAGE_PATH, usage_guide
    usage_path = project / USAGE_PATH
    usage_before = read_regular(usage_path)
    usage_after = usage_guide(project, interpreter)
    if usage_before is not None and usage_before != usage_after:
        raise ValueError("The existing local helper guide differs; initialization never overwrites a user guide.")
    changes.append((usage_path, usage_before, usage_after))
    if with_workflow:
        from .workflow import CLIENTS, skill_bytes
        source = skill_bytes()
        for selected in clients:
            skill_relative, instruction_relative = CLIENTS[selected]
            path = project / skill_relative
            before = read_regular(path)
            if before is not None and before != source:
                raise ValueError("An existing compute skill differs; initialization never overwrites a skill.")
            if before is None:
                private_paths.append(skill_relative)
            changes.append((path, before, source))
            instructions = project / instruction_relative
            before = read_regular(instructions)
            changes.append((instructions, before, _workflow_instructions(before, selected)))
    ignore_before = read_regular(ignore_path)
    ignore_text = (ignore_before or b"").decode("utf-8")
    ignore_paths = ["/.compute-relay/", "/.compute-relay/project.key",
                    *["/" + (CONFIG_PATH if selected == "codex" else CLAUDE_CONFIG_PATH).as_posix() for selected in clients]]
    ignore_block = "\n# Local Compute Relay credentials and MCP configuration\n" + "\n".join(ignore_paths) + "\n"
    ignore_after = ignore_before if ignore_text.endswith(ignore_block) else (ignore_text
        + ("\n" if ignore_text and not ignore_text.endswith("\n") else "") + ignore_block).encode("utf-8")
    _check_tracked(project, private_paths)
    if reuse_key and (key_file is not None or api_key is not None):
        raise ValueError("Use --reuse-key without importing or supplying another API key.")
    if key_file is not None and api_key is not None:
        raise ValueError("Choose a private key file or an interactive API key, not both.")
    if reuse_key:
        imported = read_regular(key_path, private=True)
        if imported is None:
            raise ValueError("This project has no existing private API key to reuse.")
        api_key = imported.decode("utf-8").strip()
    elif key_file is not None:
        imported = read_regular(absolute_path(key_file), private=True)
        if imported is None:
            raise ValueError("The API-key import file does not exist.")
        api_key = imported.decode("utf-8").strip()
    if api_key is None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            api_key = getpass.getpass("Project API key (hidden; created in the broker portal): ").strip()
    api_key = validate_key(api_key)
    created, modified = [], []
    try:
        ensure_directory(project, created)
        ensure_directory(key_path.parent, created)
        for path, _before, _after in changes:
            ensure_directory(path.parent, created)
        # Install exclusions before a credential can appear in the project directory.
        for path, before, after in [(ignore_path, ignore_before, ignore_after), *changes]:
            if before != after:
                _write(path, before, after, created)
                if before is not None:
                    modified.append((path, before, after))
        if not reuse_key:
            _write(key_path, None, (api_key + "\n").encode("utf-8"), created)
    except BaseException:
        for path, before, after in reversed(modified):
            try:
                _write(path, after, before, [])
            except (OSError, ValueError):
                pass  # Preserve concurrent user edits rather than force a rollback.
        for path in reversed(created):
            try:
                path.rmdir() if path.is_dir() else path.unlink()
            except OSError:
                pass
        raise
    return project


def main(args):
    try:
        project = initialize(args.directory, broker_url=args.broker_url, key_file=args.key_file,
            client=getattr(args, "client", "codex"), with_workflow=getattr(args, "with_workflow", False),
            reuse_key=getattr(args, "reuse_key", False))
    except (UnicodeError, OSError, getpass.GetPassWarning, EOFError):
        # These exceptions can contain paths or TOML source; never print imported contents.
        raise SystemExit("MCP setup could not finish. Check the broker URL, private key-file permissions, existing configuration and path safety. Use an interactive terminal to paste the key.") from None
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:
        raise SystemExit("MCP setup cancelled.") from None
    print(f"Compute Relay MCP connection created in {project}")
    from .workflow import USAGE_PATH
    print(f"Local helper commands: {project / USAGE_PATH}")
    client = getattr(args, "client", "codex")
    if client == "codex":
        print("The API key is private and Git exclusions are installed. Start a new trusted Codex chat in this project.")
    else:
        print("The API key is private and Git exclusions are installed. Start a new trusted "
              + ("Codex or Claude Code" if client == "both" else "Claude Code") + " session in this project.")
    if getattr(args, "with_workflow", False):
        print("The shared compute skill and managed project guidance are installed; existing tool permissions remain in force.")
