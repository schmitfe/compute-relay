"""One foreground command for durable local state, portal and SSH connectors."""

import argparse
import getpass
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from uuid import uuid4
import webbrowser

from .extensions import configured_names
from .local_state import AlreadyRunning, LocalState, LocalStateError, default_directory, validate_installation

DEFAULT_EXTENSIONS = "storage,environments,workflows"


def register_options(parser):
    parser.add_argument("--data-directory", help="Private installation state (default: $XDG_DATA_HOME/compute-relay)")
    parser.add_argument("--port", type=int, help="Loopback portal port (first-launch default: 8768)")
    parser.add_argument("--extensions", help="Bundled modules, comma-separated (first-launch default: storage,environments,workflows; empty for core only)")
    parser.add_argument("--registry-directory", help="Explicit private connector shortcut registry; otherwise this installation's registry")
    parser.add_argument("--no-browser", action="store_true", help="Print the portal address without opening a browser")
    parser.add_argument("--no-connectors", action="store_true", help="Run the portal without starting cluster connectors")
    parser.add_argument("--non-interactive", action="store_true", help="Do not ask for portal passwords or SSH/MFA input")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--no-password", action="store_true", help="Use and save password-free access to the loopback portal")
    access.add_argument("--owner-password-file", help="Set the local portal password from a private 0600 file")
    parser.add_argument("--verbose", action="store_true", help="Show sanitized connector activity")


def _password_file(filename):
    from .connector_profiles import _path
    path = _path(filename)
    # Reuse descriptor traversal, without requiring the file's parent to be 0700.
    from .local_filesystem import read_regular
    try:
        raw = read_regular(path, private=True)
        if raw is None or len(raw) > 16384:
            raise ValueError
        value = raw.decode("utf-8").rstrip("\r\n")
        if not 1 <= len(value) <= 4096 or any(char in value for char in "\x00\r\n"):
            raise ValueError
        return value
    except (OSError, ValueError, UnicodeError):
        raise LocalStateError("The owner password file must be a private regular UTF-8 file owned by you (0600), without links.") from None


def configure(state, args):
    current = state.read("installation.json")
    if current is None:
        if (state.directory / "control.sqlite3").exists():
            raise LocalStateError("A database exists without installation metadata. Restore its metadata or choose a new state directory.")
        current = {"schema_version": "1.0", "installation_id": str(uuid4()), "owner_id": str(uuid4()),
            "portal_auth": "password", "port": 8768, "extensions": list(configured_names(DEFAULT_EXTENSIONS)), "initialized": False}
    else:
        current = validate_installation(current)
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            raise LocalStateError("Choose a portal port between 1 and 65535.")
        if current["initialized"] and args.port != current["port"]:
            raise LocalStateError("Reuse this installation's saved port; its MCP connections and cluster connectors depend on that address.")
        current["port"] = args.port
    if args.extensions is not None:
        try:
            current["extensions"] = list(configured_names(args.extensions))
        except ValueError as error:
            raise LocalStateError(str(error)) from None
    password = None
    change = not current["initialized"]
    if args.no_password:
        change = not current["initialized"] or current["portal_auth"] != "none"
        current["portal_auth"] = "none"
    elif args.owner_password_file:
        password = _password_file(args.owner_password_file)
        current["portal_auth"], change = "password", True
    elif not current["initialized"]:
        if args.non_interactive or not sys.stdin.isatty():
            raise LocalStateError("First launch needs a terminal, --no-password, or --owner-password-file PATH.")
        password = getpass.getpass("Portal password (leave blank for password-free local access): ")
        if not password:
            password, current["portal_auth"] = None, "none"
        elif len(password) > 4096 or "\x00" in password:
            raise LocalStateError("Use a portal password of at most 4096 characters.")
        elif getpass.getpass("Confirm portal password: ") != password:
            raise LocalStateError("The portal passwords did not match.")
    return current, {"set_password": change, "password": password, "initialized": current["initialized"]}


def service_environment(state, installation, instance_id):
    # Ambient central-server/database/credential settings cannot select another
    # installation. The launcher supplies every local service setting explicitly.
    environment = {key: value for key, value in os.environ.items()
        if not key.startswith("BROKER_") and key != "DJANGO_SETTINGS_MODULE"}
    origin = f"http://127.0.0.1:{installation['port']}"
    environment.update(BROKER_DATA_DIR=str(state.directory), BROKER_PROFILE="local",
        BROKER_DB_ENGINE="sqlite", BROKER_DB_PATH=str(state.directory / "control.sqlite3"),
        BROKER_ARTIFACT_ROOT=str(state.directory / "objects"), BROKER_PORT=str(installation["port"]),
        BROKER_HOST="127.0.0.1", BROKER_ALLOWED_HOSTS="127.0.0.1",
        BROKER_EXTENSIONS=",".join(installation["extensions"]), BROKER_LOCAL_RUNTIME="1",
        BROKER_LOCAL_OWNER_ID=installation["owner_id"], BROKER_LOCAL_AUTH_MODE=installation["portal_auth"],
        BROKER_LOCAL_ORIGIN=origin, BROKER_INSTALLATION_ID=installation["installation_id"],
        BROKER_INSTANCE_ID=instance_id, PYTHONUNBUFFERED="1")
    return environment


def _health(origin, installation_id, instance_id):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(origin + "/healthz", timeout=0.5) as response:
            data = json.loads(response.read(4097))
        return (isinstance(data, dict) and data.get("status") == "ok" and data.get("installation_id") == installation_id
            and data.get("instance_id") == instance_id)
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _open_browser(origin, args):
    if not args.no_browser:
        try:
            webbrowser.open(origin)
        except (OSError, webbrowser.Error):
            print("Open the portal address above in your browser.", flush=True)


def _stop_server(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5)


def _run_locked(state, args):
    installation, bootstrap = configure(state, args)
    origin = f"http://127.0.0.1:{installation['port']}"
    with socket.socket() as probe:
        # Match the server's reuse setting so recent HTTP connections in
        # TIME_WAIT do not prevent an immediate, same-state restart.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", installation["port"]))
        except OSError:
            raise LocalStateError("The portal port is already in use. Stop its current application, or choose another port for a new installation.") from None
    instance_id = str(uuid4())
    environment = service_environment(state, installation, instance_id)
    os.umask(0o077)
    # A pending record makes interrupted first initialization safely resumable.
    state.write("installation.json", installation)
    print("Preparing local SQLite state…", flush=True)
    with state.log("startup.log") as log:
        try:
            initialized = subprocess.run([sys.executable, "-I", "-m", "compute_relay.local_bootstrap"],
                input=json.dumps(bootstrap).encode(), stdout=log, stderr=log, env=environment, timeout=120)
        except subprocess.TimeoutExpired:
            raise LocalStateError("Database initialization timed out. See the private startup.log in the state directory.") from None
    bootstrap.clear()
    if initialized.returncode:
        raise LocalStateError("Could not initialize local state. See the private startup.log in the state directory.")
    installation["initialized"] = True
    state.write("installation.json", installation)
    process = None
    stop = threading.Event()
    previous_handlers = {}

    def interrupted(signum, frame):
        if stop.is_set():
            return
        stop.set()
        raise KeyboardInterrupt

    try:
        for kind in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[kind] = signal.signal(kind, interrupted)
        with state.log("web.log") as log:
            process = subprocess.Popen([sys.executable, "-I", "-m", "compute_relay.local_web"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=environment, start_new_session=True)
        deadline = time.monotonic() + 20
        while not _health(origin, installation["installation_id"], instance_id):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise LocalStateError("The web server did not become ready. See the private web.log in the state directory.")
            time.sleep(0.1)
        state.write("runtime.json", {"schema_version": "1.0", "origin": origin,
            "installation_id": installation["installation_id"], "instance_id": instance_id, "pid": os.getpid()})
        print(f"Compute Relay is ready: {origin}/", flush=True)
        print(f"State: {state.directory}", flush=True)
        portal_url = origin
        if installation["portal_auth"] == "none":
            from .portal_tokens import issue
            portal_url = issue(state, origin, instance_id)
            print(f"Portal access (one use, valid for 5 minutes): {portal_url}", flush=True)
        else:
            print("Portal account: local (your saved password).", flush=True)
        print("Press Ctrl+C to stop the local application; submitted cluster jobs keep their recorded state.", flush=True)
        _open_browser(portal_url, args)
        if args.no_connectors:
            while not stop.wait(0.25):
                if process.poll() is not None:
                    raise LocalStateError("The web server stopped. See the private web.log in the state directory.")
        else:
            from .local_connectors import run_connectors
            run_connectors(origin, state.directory, registry_directory=args.registry_directory,
                extensions=installation["extensions"],
                verbose=args.verbose, non_interactive=args.non_interactive, stop_event=stop,
                server_alive=lambda: process.poll() is None)
            if process.poll() is not None:
                raise LocalStateError("The web server stopped. See the private web.log in the state directory.")
    except KeyboardInterrupt:
        print("Stopping Compute Relay…", flush=True)
    finally:
        stop.set()
        _stop_server(process)
        for kind, handler in previous_handlers.items():
            signal.signal(kind, handler)


def run(args):
    try:
        with LocalState(args.data_directory or default_directory()) as state:
            try:
                with state.lock():
                    _run_locked(state, args)
            except AlreadyRunning:
                running = state.read("runtime.json") or {}
                origin = running.get("origin", "")
                # Never follow a URL from stale or malformed runtime metadata.
                saved = state.read("installation.json")
                if saved is None or saved.get("initialized") is not True:
                    raise LocalStateError("This installation is already starting in another terminal. Complete its setup there.") from None
                installation = validate_installation(saved)
                expected_origin = f"http://127.0.0.1:{installation['port']}"
                if (origin != expected_origin or not _health(expected_origin,
                        installation["installation_id"], running.get("instance_id"))):
                    raise LocalStateError("This installation is already starting or stopping in another terminal. Wait for it to finish.") from None
                print(f"Compute Relay is already running: {expected_origin}/", flush=True)
                portal_url = expected_origin
                if installation["portal_auth"] == "none":
                    from .portal_tokens import issue
                    portal_url = issue(state, expected_origin, running["instance_id"])
                    print(f"Portal access (one use, valid for 5 minutes): {portal_url}", flush=True)
                _open_browser(portal_url, args)
    except KeyboardInterrupt:
        print("Local startup cancelled.", flush=True)
    except (LocalStateError, OSError) as error:
        print(f"Compute Relay: {error}", file=sys.stderr)
        raise SystemExit(1) from None
