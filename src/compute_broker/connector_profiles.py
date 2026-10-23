"""Private, local names for existing personal connector state directories.

Only shortcut names and paths live here. SSH identities, broker credentials and
interactive login remain entirely with the foreground personal connector.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys


class ConnectorProfileError(ValueError):
    """An actionable diagnostic containing no connector-state contents."""


@dataclass(frozen=True)
class ConnectorProfile:
    name: str
    state_directory: str


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise ConnectorProfileError("Use a shortcut name of 1–64 letters, digits, dots, underscores or hyphens.")
    return value


def _path(value):
    value = os.fspath(value)
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConnectorProfileError("Use an accessible local directory path without control characters.")
    # absolute() normalizes a relative CLI path but deliberately does not follow
    # symlinks; directory validation below rejects them at every level.
    return Path(os.path.abspath(os.path.expanduser(value)))


def default_directory():
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured and os.path.isabs(configured) else Path.home() / ".config"
    return base / "compute-broker"


def _directory(path, *, create):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_uid = os.fstat(descriptor).st_uid
    try:
        if path == Path("/"):
            raise ConnectorProfileError("Choose a private connector shortcut directory, not the filesystem root.")
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            if index == len(parts) - 1:
                if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise ConnectorProfileError("The connector shortcut directory must be owned by you with permissions 0700.")
            elif metadata.st_mode & 0o022 and not (metadata.st_uid == root_uid and metadata.st_mode & stat.S_ISVTX):
                raise ConnectorProfileError("A parent of the connector shortcut directory allows writes by other users.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _private_file(directory_fd, filename, flags):
    fd = os.open(filename, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory_fd)
    metadata = os.fstat(fd)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
        os.close(fd)
        raise ConnectorProfileError("Connector shortcut files must be owned by you, regular, private (0600), and without hard links.")
    return fd


def _state_directory(value):
    from compute_broker.worker.ssh_session import SessionStorageError, open_session_directory
    path = _path(value)
    try:
        descriptor = open_session_directory(path)
    except FileNotFoundError:
        raise ConnectorProfileError("The saved connector directory is missing. Pair the connector first, then add its directory.") from None
    except SessionStorageError as error:
        raise ConnectorProfileError(str(error)) from None
    except OSError:
        raise ConnectorProfileError("Cannot access saved connector state. Check its directory and permissions.") from None
    try:
        # Validate that this is a paired connector without opening or reading its
        # saved SSH configuration or worker token.
        metadata = os.stat("state.json", dir_fd=descriptor, follow_symlinks=False)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
            raise ConnectorProfileError("Saved connector state must be a private (0600), owned regular file without links.")
    except FileNotFoundError:
        raise ConnectorProfileError("This directory has no saved connector state. Complete its first pairing before adding a shortcut.") from None
    except OSError:
        raise ConnectorProfileError("Cannot access saved connector state. Check its directory and permissions.") from None
    finally:
        os.close(descriptor)
    return str(path)


class ConnectorProfiles:
    def __init__(self, directory=None):
        self.directory = _path(directory if directory is not None else default_directory())

    @contextmanager
    def _open(self, *, create=False, write=False):
        directory_fd = None
        lock_fd = None
        try:
            directory_fd = _directory(self.directory, create=create)
            if write:
                lock_fd = _private_file(directory_fd, "connectors.lock", os.O_RDWR | os.O_CREAT)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield directory_fd
        except ConnectorProfileError:
            raise
        except OSError:
            raise ConnectorProfileError("Cannot access connector shortcuts. Check the directory, permissions and whether any path is a symbolic link.") from None
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    @staticmethod
    def _read(directory_fd):
        try:
            fd = _private_file(directory_fd, "connectors.json", os.O_RDONLY)
        except FileNotFoundError:
            return []
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(64 * 1024 + 1)
        try:
            if len(raw) > 64 * 1024:
                raise ValueError
            document = json.loads(raw)
            if (not isinstance(document, dict) or set(document) != {"schema_version", "connectors"}
                    or document["schema_version"] != "1.0" or not isinstance(document["connectors"], list)
                    or len(document["connectors"]) > 256):
                raise ValueError
            result = []
            for entry in document["connectors"]:
                if not isinstance(entry, dict) or set(entry) != {"name", "state_directory"}:
                    raise ValueError
                name = _name(entry["name"])
                path = entry["state_directory"]
                if not isinstance(path, str) or str(_path(path)) != path:
                    raise ValueError
                result.append(ConnectorProfile(name, path))
            if len({entry.name for entry in result}) != len(result) or len({entry.state_directory for entry in result}) != len(result):
                raise ValueError
            return sorted(result, key=lambda entry: (entry.name.casefold(), entry.name))
        except (ValueError, TypeError, KeyError):
            raise ConnectorProfileError("The connector shortcut registry is invalid. Restore it or choose another registry directory.") from None

    @staticmethod
    def _write(directory_fd, profiles):
        document = {"schema_version": "1.0", "connectors": [
            {"name": profile.name, "state_directory": profile.state_directory}
            for profile in sorted(profiles, key=lambda entry: (entry.name.casefold(), entry.name))]}
        raw = json.dumps(document, indent=2).encode() + b"\n"
        if len(profiles) > 256 or len(raw) > 64 * 1024:
            raise ConnectorProfileError("The connector shortcut registry is full; remove unused shortcuts first.")
        temporary = ".connectors-" + secrets.token_hex(16)
        fd = _private_file(directory_fd, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, "connectors.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def list(self):
        # Listing a pristine installation should not create files or directories.
        try:
            self.directory.lstat()
        except FileNotFoundError:
            return []
        except OSError:
            raise ConnectorProfileError("Cannot access the connector shortcut directory. Check its path and permissions.") from None
        with self._open() as directory_fd:
            return self._read(directory_fd)

    def add(self, name, state_directory):
        profile = ConnectorProfile(_name(name), _state_directory(state_directory))
        with self._open(create=True, write=True) as directory_fd:
            existing = self._read(directory_fd)
            for current in existing:
                if current == profile:
                    return profile
                if current.name == name:
                    raise ConnectorProfileError("That shortcut name is already registered. Remove it before changing its directory.")
                if current.state_directory == profile.state_directory:
                    raise ConnectorProfileError(f"That connector directory already has the shortcut '{current.name}'.")
            self._write(directory_fd, [*existing, profile])
        return profile

    def remove(self, name):
        _name(name)
        with self._open(write=True) as directory_fd:
            existing = self._read(directory_fd)
            if not any(profile.name == name for profile in existing):
                raise ConnectorProfileError("No saved connector has that shortcut name. Use 'compute-broker connectors list'.")
            self._write(directory_fd, [profile for profile in existing if profile.name != name])

    def get(self, name):
        _name(name)
        for profile in self.list():
            if profile.name == name:
                _state_directory(profile.state_directory)
                return profile
        raise ConnectorProfileError("No saved connector has that shortcut name. Use 'compute-broker connectors list'.")


def _selected_profile(registry, name):
    if name:
        return registry.get(name)
    profiles = registry.list()
    if not profiles:
        raise ConnectorProfileError("No connector shortcuts yet. Add one with 'compute-broker connectors add NAME --state-directory PATH'.")
    if not sys.stdin.isatty():
        raise ConnectorProfileError("Specify the connector name when no interactive terminal is available.")
    print("Choose the cluster connector to start in this terminal:")
    for index, profile in enumerate(profiles, 1):
        print(f"  {index}. {profile.name} — {profile.state_directory}")
    try:
        answer = input("Connector number or name: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise ConnectorProfileError("Connector selection cancelled.") from None
    numbered = {str(index): profile.name for index, profile in enumerate(profiles, 1)}
    if answer in numbered:
        return registry.get(numbered[answer])
    return registry.get(answer)


def _connector_arguments(args, profile):
    return argparse.Namespace(setup=None, state_directory=profile.state_directory,
        identity_file=None, known_hosts=None, choose_identity=False,
        non_interactive=args.non_interactive, allow_insecure_http=False,
        once=args.once, verbose=args.verbose, connector_label=profile.name)


def run(args):
    try:
        registry = ConnectorProfiles(args.registry_directory)
        if args.connector_action == "add":
            profile = registry.add(args.name, args.state_directory)
            print(f"Saved connector '{profile.name}'. Start it with: compute-broker connectors start {profile.name}")
        elif args.connector_action == "list":
            profiles = registry.list()
            if not profiles:
                print("No connector shortcuts yet. Add one with: compute-broker connectors add NAME --state-directory PATH")
            for profile in profiles:
                print(f"{profile.name}\t{profile.state_directory}")
        elif args.connector_action == "remove":
            registry.remove(args.name)
            print(f"Removed shortcut '{args.name}'. Its connector state and cluster configuration are preserved.")
        elif args.connector_action == "start":
            if args.all_connectors:
                selected = registry.list()
                if not selected:
                    raise ConnectorProfileError("No connector shortcuts yet. Add one with 'compute-broker connectors add NAME --state-directory PATH'.")
                # Validate every path before the first SSH prompt. Actual saved
                # states are opened only by the foreground connector runner.
                for profile in selected:
                    _state_directory(profile.state_directory)
                print("Starting all saved connectors. SSH authentication runs one cluster at a time in this terminal.", flush=True)
                for profile in selected:
                    print(f"  {profile.name} — {profile.state_directory}", flush=True)
                from compute_broker.worker.personal_connector import main_many
                main_many([_connector_arguments(args, profile) for profile in selected])
                return
            profile = _selected_profile(registry, args.name)
            print(f"Starting connector '{profile.name}' in this terminal.", flush=True)
            print(f"Saved state directory: {profile.state_directory}", flush=True)
            print("Complete the SSH key passphrase or cluster MFA prompt for this connector here.", flush=True)
            from compute_broker.worker.personal_connector import main
            main(_connector_arguments(args, profile))
    except ConnectorProfileError as error:
        print(f"Connector shortcut: {error}", file=sys.stderr)
        raise SystemExit(1) from None


def register_commands(subparsers):
    parser = subparsers.add_parser("connectors", help="Save names for your existing personal connectors and start them locally")
    parser.add_argument("--registry-directory", help="Private shortcut directory (default: $XDG_CONFIG_HOME/compute-broker)")
    actions = parser.add_subparsers(dest="connector_action", required=True)
    add = actions.add_parser("add", help="Name an already paired personal connector")
    add.add_argument("name")
    add.add_argument("--state-directory", required=True)
    actions.add_parser("list", help="List saved connector names and local state directories")
    remove = actions.add_parser("remove", help="Forget a shortcut while preserving connector state")
    remove.add_argument("name")
    start = actions.add_parser("start", help="Start a named connector in this terminal, or choose from a list")
    selector = start.add_mutually_exclusive_group()
    selector.add_argument("name", nargs="?")
    selector.add_argument("--all", dest="all_connectors", action="store_true", help="Authenticate saved connectors one at a time, then serve them together")
    start.add_argument("--verbose", "-v", action="store_true", help="Show connector activity without command contents or credentials")
    start.add_argument("--non-interactive", action="store_true", help="Fail if SSH requires a prompt")
    start.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(run=run)
