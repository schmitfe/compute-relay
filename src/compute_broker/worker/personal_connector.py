"""Personal connector bootstrap: local SSH capability, one-use broker pairing."""

import asyncio
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time
from uuid import UUID

from .client import ControlError, JsonClient, strict_json
from .enrollment import profile_metadata


class ConnectorStartupError(ValueError):
    MESSAGES = {
        "PRIVATE_FILE_UNREADABLE": "Cannot read the setup file or saved connector state. Check that the file exists and is accessible to you.",
        "PRIVATE_FILE_REQUIRED": "Connector files must be owned by you, regular, without hard links and private (0600).",
        "SETUP_EXPIRED": "Pairing expired; download a fresh connector setup from the cluster page.",
        "ALREADY_PAIRED": "This connector is already paired. Resume without --setup, or use a new state directory.",
        "PAIRING_UNCERTAIN": "A previous pairing outcome is uncertain. Download a new setup and choose a new state directory.",
    }

    def __init__(self, code):
        self.code = code
        super().__init__(self.MESSAGES[code])


def read_private(path, *, limit=64 * 1024):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ConnectorStartupError("PRIVATE_FILE_UNREADABLE") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                or info.st_uid != os.getuid()):
            raise ConnectorStartupError("PRIVATE_FILE_REQUIRED")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Connector file exceeds its size limit.")
    return strict_json(data)


def write_private(path, document):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(document, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def local_path(value):
    if (not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or not (value.startswith("/") or value.startswith("~/"))):
        raise ValueError("Use an explicit absolute or ~/ local path.")
    return str(Path(value).expanduser().absolute())


def prepare(setup, state_directory, *, identity_file=None, known_hosts=None, allow_insecure_http=False,
            choose_identity=False, allow_picker=False):
    """Freeze local runtime before authentication; no private key bytes are read."""
    if (not isinstance(setup, dict) or setup.get("schema_version") != "1.0"
            or set(setup) != {"schema_version", "broker_url", "registration_id", "owner_id", "name", "pairing_token", "expires_at", "target", "remote_root", "local", "site_profile"}):
        raise ValueError("Invalid personal connector setup.")
    identifier = str(UUID(setup["registration_id"]))
    owner = str(UUID(setup["owner_id"]))
    JsonClient(setup["broker_url"], setup["pairing_token"], allow_insecure_http=allow_insecure_http)
    from datetime import datetime, timezone
    if datetime.fromisoformat(setup["expires_at"]) <= datetime.now(timezone.utc):
        raise ConnectorStartupError("SETUP_EXPIRED")
    hints = setup["local"]
    if (not isinstance(hints, dict) or set(hints) - {"identity_file", "known_hosts", "session_seconds", "module_availability_enabled"}
            or type(hints.get("module_availability_enabled", False)) is not bool):
        raise ValueError("Invalid local connector hints.")
    ssh = setup["target"]
    if not isinstance(ssh, dict) or set(ssh) != {"host", "user", "port"}:
        raise ValueError("Invalid connector target.")
    selected = identity_file or hints.get("identity_file")
    if choose_identity or (not selected and allow_picker):
        from .file_picker import choose_identity_file
        selected = choose_identity_file()
    key = local_path(selected)
    hosts = local_path(known_hosts or hints.get("known_hosts") or str(state_directory / "known_hosts"))
    private = {"backend": "slurm", "remote_root": setup["remote_root"], "python_path": "/usr/bin/python3",
        "ssh": {**ssh, "mode": "key", "identity_file": key, "known_hosts": hosts}}
    from compute_broker.transports.openssh import OpenSSHTransport
    OpenSSHTransport(private)
    lifetime = hints.get("session_seconds", 28800)
    if type(lifetime) is not int or not 60 <= lifetime <= 86400:
        raise ValueError("Session lifetime must be 60–86400 seconds.")
    return {"schema_version": "1.0", "owner_id": owner, "registration_id": identifier,
        "broker_url": setup["broker_url"], "connection": private, "session_seconds": lifetime,
        "allow_insecure_http": allow_insecure_http, "site_profile": setup["site_profile"],
        "module_discovery_mode": "auto", "module_availability_enabled": hints.get("module_availability_enabled", False)}


def runtime_profile(state, session):
    connection = session.runtime_configuration()
    connection["remote_root"] = state["connection"]["remote_root"]
    alias = "personal-" + UUID(state["registration_id"]).hex[:16]
    profile = {"backend": "slurm", "allowed_hosts": [connection["ssh"]["host"]],
        "allowed_users": [connection["ssh"]["user"]], "allowed_ports": [connection["ssh"]["port"]],
        "remote_root_prefixes": [connection["remote_root"]], "profile_revision": 1,
        "site_profile": state["site_profile"], "python_path": connection["python_path"],
        "ssh": {key: value for key, value in connection["ssh"].items() if key not in {"host", "user", "port"}}}
    profile["profile_revision"] = state.get("profile_generation", 1)
    if state.get("module_discovery_mode") == "auto":
        profile["environment_discovery"] = {"module_inventory_mode": "auto"}
        if state.get("module_availability_enabled") is True:
            profile["environment_discovery"]["module_availability_enabled"] = True
    return alias, profile


def pair(setup, state, session, *, client_factory=JsonClient):
    alias, profile = runtime_profile(state, session)
    result = client_factory(state["broker_url"], setup["pairing_token"], allow_insecure_http=state.get("allow_insecure_http", False)).request(
        "POST", "/internal/v1/connectors/enroll", {"profile": profile_metadata(alias, profile)})
    if str(UUID(result["owner_id"])) != state["owner_id"]:
        raise ValueError("Pairing returned a different owner.")
    # Validate before saving durable local worker credentials.
    JsonClient(state["broker_url"], result["worker_token"], allow_insecure_http=state.get("allow_insecure_http", False))
    str(UUID(result["worker_id"]));str(UUID(result["connection_id"]))
    return {**state, "worker_id": result["worker_id"], "worker_token": result["worker_token"], "connection_id": result["connection_id"]}


async def serve(state, session, state_directory, *, once=False, deadline=None):
    from .async_client import AsyncJsonClient
    from .personal_setup import PersonalWorker
    from .journal import Journal
    alias, profile = runtime_profile(state, session)
    client = AsyncJsonClient(state["broker_url"], state["worker_token"], allow_insecure_http=state.get("allow_insecure_http", False), max_response_bytes=384 * 1024 * 1024)
    worker = PersonalWorker(client, {}, Journal(state_directory / "journal"), credential_profiles={alias: profile},
        extensions=[], owner_id=state["owner_id"], state=state, session=session, state_directory=state_directory)
    try:
        remaining = max(0, deadline - time.monotonic()) if deadline is not None else state["session_seconds"]
        async with asyncio.timeout(remaining):
            try:
                await worker.hello()
            except ControlError as error:
                if error.status == 401:
                    print("Connector access was withdrawn. Sign in to the portal before reconnecting.", file=sys.stderr)
                    return
                raise
            while True:
                if not session.check():
                    print("SSH session ended. Restart the connector to sign in again.", file=sys.stderr)
                    return
                try:
                    worked = await worker.setup_once() or await worker.poll_once()
                except ControlError as error:
                    if error.status in {401, 403}:
                        print("Connector access was withdrawn. Sign in to the portal before reconnecting.", file=sys.stderr)
                        return
                    worked = False
                except Exception:
                    print("Operation interrupted; retained submission state will be reconciled.", file=sys.stderr)
                    worked = False
                if once:
                    return
                await asyncio.sleep(1 if worked else 2)
    except TimeoutError:
        print("SSH session lifetime reached. Restart the connector to sign in again.", file=sys.stderr)
    finally:
        await client.close()


def run(args):
    from .ssh_session import SSHSession, open_session_directory
    directory = Path(args.state_directory).expanduser().absolute()
    # Prevent two connector processes from consuming the same worker identity.
    # Validate and create every ancestor before key selection or lock creation.
    import fcntl
    directory_fd = open_session_directory(directory, create=True)
    try:
        fd = os.open("lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077):
            raise ConnectorStartupError("PRIVATE_FILE_REQUIRED")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / "state.json"
        if args.setup and path.exists():
            raise ConnectorStartupError("ALREADY_PAIRED")
        setup = read_private(args.setup) if args.setup else None
        choose_identity = getattr(args, "choose_identity", False)
        if choose_identity and (args.non_interactive or not setup):
            raise ValueError("Choose a key during interactive first pairing only.")
        state = prepare(setup, directory, identity_file=args.identity_file, known_hosts=args.known_hosts,
            allow_insecure_http=args.allow_insecure_http, choose_identity=choose_identity,
            allow_picker=not args.non_interactive) if setup else read_private(path)
        if not setup and (args.identity_file or args.known_hosts):
            raise ValueError("Changing a paired connector requires a fresh setup and a new state directory.")
        # A journal intent survives interruption; a consumed pairing response
        # cannot be replayed to recover a lost secret. Re-pair explicitly instead.
        if setup and (directory / "pairing-pending.json").exists():
            raise ConnectorStartupError("PAIRING_UNCERTAIN")
        session = SSHSession(state["connection"], directory, lifetime_seconds=state["session_seconds"])
        try:
            print("Connecting to your cluster. Complete any OpenSSH host-key, passphrase or MFA prompt in this terminal.")
            session.start(interactive=not args.non_interactive)
            deadline = time.monotonic() + state["session_seconds"]
            if setup:
                if not state["connection"].get("remote_root"):
                    from .personal_setup import default_work_root
                    state["connection"]["remote_root"] = asyncio.run(default_work_root(session))
                write_private(directory / "pairing-pending.json", {"registration_id": state["registration_id"]})
                state = pair(setup, state, session)
                write_private(path, state)
                (directory / "pairing-pending.json").unlink()
                print("Connector paired automatically. Cluster identification is queued.")
            asyncio.run(serve(state, session, directory, once=args.once, deadline=deadline))
        finally:
            session.close()
    finally:
        os.close(fd)


def main(args):
    from .file_picker import FileSelectionCancelled, FileSelectionUnavailable
    from .ssh_session import SessionStorageError
    from compute_broker.transports.openssh import TransportError
    previous = signal.getsignal(signal.SIGTERM)
    def stopped(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stopped)
    try:
        run(args)
    except KeyboardInterrupt:
        print("Connector stopped; submitted Slurm jobs retain their recorded state.")
    except FileSelectionCancelled as error:
        print(str(error))
    except FileSelectionUnavailable as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except (ConnectorStartupError, SessionStorageError) as error:
        print(f"Connector could not start [{error.code}]: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except BlockingIOError:
        print("Connector could not start [STATE_LOCKED]: Another connector is using this state directory. Stop it before retrying.", file=sys.stderr)
        raise SystemExit(1) from None
    except TransportError:
        print("Connector could not start [SSH_LOGIN_FAILED]: Check the SSH connection settings and any OpenSSH message above.", file=sys.stderr)
        raise SystemExit(1) from None
    except ControlError:
        print("Connector could not start [BROKER_PAIRING_FAILED]: Check that the broker is reachable. A pairing request may already have been accepted; download a new setup and use a new state directory for another first pairing.", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # Do not print exceptions that may include a setup token or SSH details.
        print("Connector could not start. Check private file permissions, the SSH login, setup expiry and whether this state directory is already in use.", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        signal.signal(signal.SIGTERM, previous)
