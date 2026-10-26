"""Personal connector supervision for the foreground, single-user application.

OpenSSH authentication stays on the launcher's main thread. Already connected
workers serve asynchronously on a separate event loop while the next cluster
asks for its passphrase or second factor. Failed or expired logins require an
explicit new start request; the supervisor never repeats MFA automatically.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .connector_profiles import ConnectorProfileError, ConnectorProfiles, _directory, _path, _private_file
from .domain.validation import strict_json
from .worker.connector_status import display_label


class LocalConnectorError(ValueError):
    """Fixed, non-secret diagnostics safe for the portal and terminal."""


def loopback_origin(value):
    """Accept a single, credential-free loopback origin without URL expansion."""
    try:
        if not isinstance(value, str) or any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError
        parsed = urlsplit(value)
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
        if (not loopback or parsed.scheme not in {"http", "https"} or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or parsed.port == 0):
            raise ValueError
    except (ValueError, TypeError):
        raise LocalConnectorError("Local connector startup requires a loopback broker origin.") from None
    return value.rstrip("/")


def connector_directory(data_directory, registration_id):
    # UUID parsing is the only source of this path component. A short fixed
    # directory name leaves room for OpenSSH's Unix-socket path length limit.
    return _path(data_directory) / "c" / UUID(str(registration_id)).hex


def _validate_request(document, expected_origin=None):
    fields = {"schema_version", "request_id", "registration_id", "broker_url", "name", "target", "setup"}
    if not isinstance(document, dict) or set(document) != fields or document["schema_version"] != "1.0":
        raise LocalConnectorError("The local connector start request is invalid.")
    try:
        UUID(document["request_id"])
        UUID(document["registration_id"])
        origin = loopback_origin(document["broker_url"])
        target = document["target"]
        if (not isinstance(document["name"], str) or not document["name"] or len(document["name"]) > 256
                or not isinstance(target, dict) or set(target) != {"host", "user", "port"}
                or not isinstance(target["host"], str) or not isinstance(target["user"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", target["host"]) or target["host"].startswith("-")
                or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}", target["user"])
                or type(target["port"]) is not int or not 1 <= target["port"] <= 65535):
            raise ValueError
        setup = document["setup"]
        if setup is not None and (not isinstance(setup, dict) or setup.get("registration_id") != document["registration_id"]
                or setup.get("broker_url", "").rstrip("/") != origin or setup.get("target") != target):
            raise ValueError
        if expected_origin is not None and origin != expected_origin:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise LocalConnectorError("The local connector start request does not match this broker or cluster.") from None
    return document


class LocalConnectorRequests:
    """Private, bounded request spool written only by human portal actions."""
    def __init__(self, data_directory):
        self.directory = _path(data_directory) / "connector-requests"

    @contextmanager
    def _locked(self):
        directory_fd = None
        lock_fd = None
        try:
            directory_fd = _directory(self.directory, create=True)
            lock_fd = _private_file(directory_fd, "requests.lock", os.O_RDWR | os.O_CREAT)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield directory_fd
        except (OSError, ConnectorProfileError):
            raise LocalConnectorError("Cannot access the private local connector queue. Check its directory and permissions.") from None
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    @staticmethod
    def _pending(directory_fd):
        return sorted(name for name in os.listdir(directory_fd) if re.fullmatch(r"[0-9a-f]{32}\.json", name))

    def enqueue(self, registration_id, factory):
        """Serialize issuance and writing so a second request cannot expire the first."""
        registration_id = str(UUID(str(registration_id)))
        filename = UUID(registration_id).hex + ".json"
        with self._locked() as directory_fd:
            pending = self._pending(directory_fd)
            if filename in pending:
                raise LocalConnectorError("A start request for this cluster is already waiting in the application terminal.")
            if len(pending) >= 64:
                raise LocalConnectorError("Too many local connector starts are waiting. Complete the terminal prompts first.")
            document = _validate_request(factory())
            if document["registration_id"] != registration_id:
                raise LocalConnectorError("The local connector start request identifies another cluster.")
            raw = json.dumps(document, ensure_ascii=True).encode("utf-8") + b"\n"
            if len(raw) > 64 * 1024:
                raise LocalConnectorError("The local connector start request exceeds its size limit.")
            temporary = ".request-" + secrets.token_hex(16)
            fd = _private_file(directory_fd, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        return document["request_id"]

    def take(self, expected_origin):
        with self._locked() as directory_fd:
            pending = self._pending(directory_fd)
            if not pending:
                return None
            # The oldest human action gets the next foreground authentication.
            filename = min(pending, key=lambda name: os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mtime_ns)
            try:
                fd = _private_file(directory_fd, filename, os.O_RDONLY)
                with os.fdopen(fd, "rb") as stream:
                    raw = stream.read(64 * 1024 + 1)
                if len(raw) > 64 * 1024:
                    raise ValueError
                document = _validate_request(strict_json(raw), expected_origin)
                if UUID(document["registration_id"]).hex + ".json" != filename:
                    raise ValueError
                return document
            except (ValueError, TypeError, KeyError, ConnectorProfileError):
                raise LocalConnectorError("An invalid local start request was discarded. Request startup again from the cluster page.") from None
            finally:
                # Never replay a request after a failed or interrupted MFA login.
                os.unlink(filename, dir_fd=directory_fd)

    @contextmanager
    def setup_file(self, request):
        """Materialize a one-use setup only while authenticating that connector."""
        from .worker.personal_connector import write_private
        filename = ".setup-" + UUID(request["request_id"]).hex + ".json"
        path = self.directory / filename
        with self._locked():
            write_private(path, request["setup"])
        try:
            yield path
        finally:
            with self._locked() as directory_fd:
                try:
                    os.unlink(filename, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass


class _ServingLoop:
    """Serve authenticated connectors while the main thread handles new SSH prompts."""
    def __init__(self, extensions):
        self.extensions = extensions
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="compute-relay-connectors", daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        from .worker.concurrency import PrincipalScheduler
        self.scheduler = PrincipalScheduler(max_concurrency=16, max_pending=256, max_per_owner=16)
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def add(self, context, connection):
        from .worker.personal_connector import serve
        state, session, directory, deadline, status = connection

        async def serve_one():
            try:
                await serve(state, session, directory, deadline=deadline, status=status, extensions=self.extensions, scheduler=self.scheduler)
            except asyncio.CancelledError:
                raise
            except Exception:
                status.problem("Connector stopped after an operation failed. Start it again explicitly from the cluster page.")
            finally:
                try:
                    # serve() has already closed its SSH master asynchronously;
                    # leaving this context releases only its local identity lock.
                    context.__exit__(None, None, None)
                except Exception:
                    status.problem("Connector cleanup was interrupted. Check the local session before restarting it.")

        return asyncio.run_coroutine_threadsafe(serve_one(), self.loop)

    def close(self):
        async def stop():
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            asyncio.run_coroutine_threadsafe(stop(), self.loop).result(timeout=30)
        except Exception:
            print("Connector shutdown is still finishing; check for remaining local SSH sessions.", file=sys.stderr, flush=True)
        finally:
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except RuntimeError:
                pass
            self.thread.join(timeout=2)


def _arguments(directory, *, label, base_url, verbose, non_interactive, registry_directory, registration_id=None, target=None, setup=None):
    return argparse.Namespace(state_directory=str(directory), connector_label=label, setup=str(setup) if setup else None,
        identity_file=None, known_hosts=None, choose_identity=False, non_interactive=non_interactive,
        allow_insecure_http=False, once=False, verbose=verbose, register_connector=True,
        registry_directory=str(registry_directory), expected_broker_url=base_url,
        expected_registration_id=registration_id, expected_target=target)


def _start(args, serving, active):
    from .worker.personal_connector import connected
    label = display_label(args.connector_label)
    path = args.state_directory
    if path in active and not active[path].done():
        print(f"[{label}] Connector is already running in this application.", flush=True)
        return
    context = connected(args)
    try:
        connection = context.__enter__()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BlockingIOError:
        print(f"[{label}] Another connector is already using this saved identity. Its existing session was left running.", file=sys.stderr, flush=True)
        return
    except Exception:
        # SSH prints its own human login diagnostics directly to the terminal.
        # Never interpolate an exception that may include a setup token.
        print(f"[{label}] Connector could not start. Check the SSH messages above and request another start explicitly; other clusters remain available.", file=sys.stderr, flush=True)
        return
    try:
        active[path] = serving.add(context, connection)
    except BaseException:
        context.__exit__(None, None, None)
        raise


def run_connectors(base_url, state_directory, registry_directory=None, verbose=False, non_interactive=False,
                   stop_event=None, server_alive=None, *, poll_interval=1.0, extensions=None):
    """Keep the single-user application's own connectors available until stopped.

    Invoke on the foreground main thread after the loopback web server is
    healthy. Registry entries are attempted once per launch; human start
    requests are the only way to retry a failed or expired connector.
    """
    from .worker.personal_connector import read_private
    from .extensions import async_worker_names, configured_names
    selected_extensions = async_worker_names(configured_names(",".join(extensions)) if extensions is not None else configured_names())
    base_url = loopback_origin(base_url)
    data_directory = _path(state_directory)
    registry = ConnectorProfiles(registry_directory or data_directory / "connectors-registry")
    requests = LocalConnectorRequests(data_directory)
    stop_event = stop_event or threading.Event()
    server_alive = server_alive or (lambda: True)
    serving = _ServingLoop(selected_extensions)
    attempted = set()
    active = {}
    registry_error = False
    queue_error = False
    try:
        while not stop_event.is_set() and server_alive():
            active = {path: future for path, future in active.items() if not future.done()}
            try:
                request = requests.take(base_url)
                queue_error = False
            except LocalConnectorError:
                if not queue_error:
                    print("A local connector start could not be read. Check the private application directory and request startup again.", file=sys.stderr, flush=True)
                queue_error = True
                request = None
            if request is not None:
                directory = connector_directory(data_directory, request["registration_id"])
                attempted.add(str(directory))
                try:
                    common = dict(label=request["name"], base_url=base_url, verbose=verbose,
                        non_interactive=non_interactive, registry_directory=registry.directory,
                        registration_id=request["registration_id"], target=request["target"])
                    if request["setup"] is None:
                        _start(_arguments(directory, **common), serving, active)
                    else:
                        with requests.setup_file(request) as setup:
                            _start(_arguments(directory, setup=setup, **common), serving, active)
                except (LocalConnectorError, OSError, ValueError):
                    print(f"[{display_label(request['name'])}] Local setup could not finish. Request a fresh start from the cluster page.", file=sys.stderr, flush=True)
                continue
            try:
                profiles = registry.list()
                registry_error = False
            except ConnectorProfileError:
                if not registry_error:
                    print("Saved connectors could not be read. Check the private connector registry; the web portal remains available.", file=sys.stderr, flush=True)
                registry_error = True
                profiles = []
            for profile in profiles:
                if stop_event.is_set() or not server_alive():
                    break
                if profile.state_directory in attempted:
                    continue
                attempted.add(profile.state_directory)
                try:
                    saved = read_private(Path(profile.state_directory) / "state.json")
                    if saved.get("broker_url", "").rstrip("/") != base_url:
                        print(f"[{display_label(profile.name)}] Saved connector belongs to another broker; it was not started.", flush=True)
                        continue
                except Exception:
                    print(f"[{display_label(profile.name)}] Saved connector is unavailable. Check its private state directory.", file=sys.stderr, flush=True)
                    continue
                _start(_arguments(profile.state_directory, label=profile.name, base_url=base_url,
                    verbose=verbose, non_interactive=non_interactive, registry_directory=registry.directory), serving, active)
            stop_event.wait(poll_interval)
    finally:
        serving.close()
