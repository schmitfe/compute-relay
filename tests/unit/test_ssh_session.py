"""Private control-socket ownership and fixed interactive OpenSSH commands."""

from copy import deepcopy
import asyncio
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from compute_relay.transports.openssh import OpenSSHTransport, TransportError
from compute_relay.worker import ssh_session
from compute_relay.worker.ssh_session import SSHSession


@pytest.fixture
def state():
    directory = Path(tempfile.mkdtemp(prefix="cbssh-"))
    yield directory / "state"
    shutil.rmtree(directory)


@pytest.fixture
def connection():
    return {"backend": "slurm", "revision_sha256": "a" * 64, "remote_root": "/srv/jobs",
        "ssh": {"host": "cluster.example.invalid", "user": "researcher", "port": 2222,
            "identity_file": "/private/cluster-key", "known_hosts": "/private/known_hosts"}}


class FakeSSH:
    def __init__(self):
        self.calls = []
        self.sockets = []
        self.start_result = 0
        self.check_result = 0
        self.exit_result = 0
        self.start_error = None
        self.control_error = None
        self.create_socket = True

    def __call__(self, arguments, **options):
        self.calls.append((arguments, options))
        if "-O" not in arguments:
            if self.create_socket:
                path = arguments[arguments.index("-S") + 1]
                master = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                master.bind(path);os.chmod(path, 0o600)
                self.sockets.append(master)
            if self.start_error is not None:
                raise self.start_error
            return SimpleNamespace(returncode=self.start_result)
        if self.control_error is not None:
            raise self.control_error
        operation = arguments[arguments.index("-O") + 1]
        return SimpleNamespace(returncode=self.check_result if operation == "check" else self.exit_result)

    def close(self):
        for descriptor in self.sockets:
            descriptor.close()


@pytest.fixture
def runner(monkeypatch):
    fake = FakeSSH()
    monkeypatch.setattr(ssh_session.subprocess, "run", fake)
    yield fake
    fake.close()


def options(arguments):
    return [arguments[index + 1] for index, value in enumerate(arguments) if value == "-o"]


def test_interactive_start_inherits_human_terminal_and_uses_only_explicit_key(connection, state, runner, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/ambient/agent")
    monkeypatch.setenv("SSH_ASKPASS", "/ambient/askpass")
    monkeypatch.setenv("DISPLAY", ":99")
    session = SSHSession(connection, state)
    assert session.start() is session
    command, execution = runner.calls[0]
    assert command[:4] == ["/usr/bin/ssh", "-F", "/dev/null", "-T"]
    assert command[-2:] == ["--", "researcher@cluster.example.invalid"]
    assert all(flag in command for flag in ("-M", "-N", "-f"))
    assert command[command.index("-i") + 1] == "/private/cluster-key"
    settings = options(command)
    for expected in ("BatchMode=no", "StrictHostKeyChecking=ask", "IdentityAgent=none", "IdentitiesOnly=yes",
            "ForwardAgent=no", "ForwardX11=no", "ForwardX11Trusted=no", "ClearAllForwardings=yes",
            "ControlPersist=no", "PermitLocalCommand=no", "ProxyJump=none", "ProxyCommand=none"):
        assert expected in settings
    assert "UserKnownHostsFile=/private/known_hosts" in settings
    assert execution == {"stdin": None, "stdout": None, "stderr": None, "check": False, "timeout": 300,
        "env": {"PATH": "/usr/bin:/bin", "LANG": "C"}}
    assert state.stat().st_mode & 0o777 == 0o700
    session.close()


def test_noninteractive_start_cannot_prompt(connection, state, runner):
    session = SSHSession(connection, state, lifetime_seconds=60).start(interactive=False)
    command, execution = runner.calls[0]
    assert "BatchMode=yes" in options(command) and "StrictHostKeyChecking=yes" in options(command)
    assert "NumberOfPasswordPrompts=0" in options(command)
    assert execution["stdin"] == execution["stdout"] == execution["stderr"] == subprocess.DEVNULL
    assert execution["timeout"] == 60
    session.close()


def test_runtime_configuration_is_stable_private_key_free_and_fail_closed(connection, state, runner):
    original = deepcopy(connection)
    session = SSHSession(connection, state)
    first = session.runtime_configuration()
    assert connection == original
    assert first["revision_sha256"] == original["revision_sha256"]
    assert first["ssh"]["mode"] == "mfa" and first["ssh"]["control_path"] == str(state / "s")
    assert "identity_file" not in first["ssh"]
    assert first == SSHSession(connection, state).runtime_configuration()
    transport = OpenSSHTransport(first)
    assert "ProxyCommand=/bin/false" in options(transport.argv())
    session.start()
    assert first == session.runtime_configuration()
    session.close()


@pytest.mark.parametrize("operation", ["check", "exit"])
def test_master_control_operations_cannot_fall_back_to_authentication(connection, state, runner, operation):
    session = SSHSession(connection, state).start()
    session.check() if operation == "check" else session.close()
    command, execution = runner.calls[-1]
    assert command[command.index("-O") + 1] == operation
    assert "-M" not in command and "-i" not in command
    assert "ProxyCommand=/bin/false" in options(command) and "ControlMaster=no" in options(command)
    assert "IdentityFile=none" in options(command) and "IdentityAgent=none" in options(command)
    assert "BatchMode=yes" in options(command)
    assert execution["stdin"] == execution["stdout"] == execution["stderr"] == subprocess.DEVNULL
    session.close()


def test_async_session_checks_and_shutdown_use_native_bounded_ssh_without_identity_fallback(connection, state, runner, monkeypatch):
    from compute_relay.transports.async_openssh import AsyncOpenSSHTransport
    session = SSHSession(connection, state).start()
    calls = []
    async def native_run(transport, arguments, payload):
        await asyncio.sleep(0)
        calls.append((arguments, payload, transport.connection))
        return b""
    monkeypatch.setattr(AsyncOpenSSHTransport, "_run", native_run)
    monkeypatch.setattr(asyncio, "to_thread", lambda *a, **kw: pytest.fail("Session control used a thread-pool SSH bridge"))
    async def scenario():
        assert await session.check_async()
        await session.close_async()
    asyncio.run(scenario())
    assert [arguments[arguments.index("-O") + 1] for arguments, _, _ in calls] == ["check", "exit"]
    for arguments, payload, configuration in calls:
        assert "-i" not in arguments and "-M" not in arguments and payload == b""
        assert "ProxyCommand=/bin/false" in options(arguments)
        assert "IdentityFile=none" in options(arguments) and "BatchMode=yes" in options(arguments)
        assert configuration["command_timeout_seconds"] == 10 and "identity_file" not in configuration["ssh"]
    assert session._directory_fd is None
    session.close()


def test_async_shutdown_finishes_owned_exit_when_cancelled(connection, state, runner, monkeypatch):
    from compute_relay.transports.async_openssh import AsyncOpenSSHTransport
    session = SSHSession(connection, state).start()
    entered, release = asyncio.Event(), asyncio.Event()
    async def native_run(*args):
        entered.set()
        await release.wait()
        return b""
    monkeypatch.setattr(AsyncOpenSSHTransport, "_run", native_run)
    async def scenario():
        task = asyncio.create_task(session.close_async())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and session._directory_fd is not None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session._directory_fd is None
    asyncio.run(scenario())


def test_recovery_removes_only_our_refused_stale_socket(connection, state, runner, monkeypatch):
    session = SSHSession(connection, state).start()
    runner.close()  # Simulate SIGKILL: the socket inode remains without a listener.
    async def dead_master(_):
        raise TransportError("master unavailable")
    monkeypatch.setattr(session, "_control_async", dead_master)
    assert asyncio.run(session.retire_async()) is True
    assert not (state / "s").exists() and session._directory_fd is None


def test_recovery_does_not_unlink_a_still_listening_master(connection, state, runner, monkeypatch):
    session = SSHSession(connection, state).start()
    runner.sockets[-1].listen()
    async def unavailable_control(_):
        raise TransportError("control unavailable")
    monkeypatch.setattr(session, "_control_async", unavailable_control)
    assert asyncio.run(session.retire_async()) is False
    assert (state / "s").exists()


def test_recovery_refuses_a_replaced_socket_or_regular_file(connection, state, runner, monkeypatch):
    session = SSHSession(connection, state).start()
    (state / "s").unlink()
    (state / "s").write_text("must be retained")
    async def no_control(_):
        pytest.fail("An unknown socket was given a control command")
    monkeypatch.setattr(session, "_control_async", no_control)
    assert asyncio.run(session.retire_async()) is False
    assert (state / "s").read_text() == "must be retained"


def test_async_reconnect_is_one_public_key_only_attempt_without_prompts(connection, state, runner, monkeypatch):
    from compute_relay.transports.async_openssh import AsyncOpenSSHTransport
    session = SSHSession(connection, state).start()
    runner.close()
    (state / "s").unlink()
    session._release_directory()
    calls = []
    async def run(transport, arguments, payload):
        calls.append((arguments, payload, transport.connection))
        if "-O" not in arguments:
            master = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            master.bind(str(state / "s")); os.chmod(state / "s", 0o600)
            runner.sockets.append(master)
        return b""
    monkeypatch.setattr(AsyncOpenSSHTransport, "_run", run)
    asyncio.run(session.reconnect_async())
    command, payload, configuration = calls[0]
    for expected in ("BatchMode=yes", "NumberOfPasswordPrompts=0", "StrictHostKeyChecking=yes",
            "PreferredAuthentications=publickey", "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
            "ConnectionAttempts=1", "IdentityAgent=none", "IdentitiesOnly=yes"):
        assert expected in options(command)
    assert command.count("-i") == 1 and payload == b""
    assert configuration["command_timeout_seconds"] == 30 and "identity_file" not in configuration["ssh"]
    assert [args[args.index("-O") + 1] for args, _, _ in calls[1:]] == ["check"]
    asyncio.run(session.close_async())


def test_async_control_never_contacts_a_replacement_socket(connection, state, runner, monkeypatch):
    from compute_relay.transports.async_openssh import AsyncOpenSSHTransport
    session = SSHSession(connection, state).start()
    path = state / "s"
    path.unlink()
    foreign = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    foreign.bind(str(path));os.chmod(path, 0o600);runner.sockets.append(foreign)
    async def forbidden(*args):
        pytest.fail("A replacement socket was contacted")
    monkeypatch.setattr(AsyncOpenSSHTransport, "_run", forbidden)
    async def scenario():
        assert await session.check_async() is False
        await session.close_async()
    asyncio.run(scenario())
    assert path.exists() and session._directory_fd is None


@pytest.mark.parametrize("failure", ["returncode", "timeout", "cancelled", "oserror", "check"])
def test_partial_master_is_closed_after_failed_or_cancelled_login(connection, state, runner, failure):
    if failure == "returncode": runner.start_result = 255
    elif failure == "timeout": runner.start_error = subprocess.TimeoutExpired("ssh", 60)
    elif failure == "cancelled": runner.start_error = KeyboardInterrupt()
    elif failure == "oserror": runner.start_error = OSError("synthetic spawn failure")
    else: runner.check_result = 255
    session = SSHSession(connection, state)
    with pytest.raises(KeyboardInterrupt if failure == "cancelled" else TransportError):
        session.start()
    assert any("-O" in command and command[command.index("-O") + 1] == "exit" for command, _ in runner.calls)
    assert not session.check()
    assert session._directory_fd is None


def test_success_without_socket_is_not_accepted(connection, state, runner):
    runner.create_socket = False
    with pytest.raises(TransportError): SSHSession(connection, state).start()
    assert len(runner.calls) == 1


@pytest.mark.parametrize("kind", ["socket", "file", "symlink", "directory"])
def test_unknown_control_path_is_not_reused_or_deleted(connection, state, runner, kind):
    state.mkdir(mode=0o700)
    existing = state / "s"
    if kind == "socket":
        foreign = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        foreign.bind(str(existing));os.chmod(existing, 0o600);runner.sockets.append(foreign)
    elif kind == "file": existing.write_text("untouched")
    elif kind == "symlink": existing.symlink_to("/nonexistent")
    else: existing.mkdir()
    before = existing.lstat()
    with pytest.raises(TransportError, match="existing unknown"): SSHSession(connection, state).start()
    assert runner.calls == [] and existing.lstat().st_ino == before.st_ino


def test_replacement_socket_is_neither_checked_closed_nor_deleted(connection, state, runner):
    session = SSHSession(connection, state).start()
    path = state / "s";path.unlink()
    foreign = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    foreign.bind(str(path));os.chmod(path, 0o600);runner.sockets.append(foreign)
    previous_calls = len(runner.calls)
    assert not session.check()
    session.close()
    assert len(runner.calls) == previous_calls and path.exists()


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_state_directory_requires_private_mode(connection, state, runner, mode):
    state.mkdir(mode=0o700);state.chmod(mode)
    with pytest.raises(TransportError): SSHSession(connection, state).start()
    assert runner.calls == []


def test_state_directory_must_belong_to_current_uid(connection, state, runner, monkeypatch):
    state.mkdir(mode=0o700)
    actual = os.geteuid();monkeypatch.setattr(ssh_session.os, "geteuid", lambda: actual + 1)
    with pytest.raises(TransportError): SSHSession(connection, state).start()
    assert runner.calls == []


def test_symlink_ancestors_are_not_followed(connection, state, runner):
    state.mkdir(mode=0o700)
    alias = state.parent / "alias";alias.symlink_to(state, target_is_directory=True)
    with pytest.raises(TransportError): SSHSession(connection, alias / "child").start()
    assert runner.calls == [] and not (state / "child").exists()


def test_permissions_or_directory_replacement_stop_control_calls(connection, state, runner):
    session = SSHSession(connection, state).start()
    before = len(runner.calls);state.chmod(0o755)
    assert not session.check()
    session.close()
    assert len(runner.calls) == before


def test_replaced_directory_stops_control_operations(connection, state, runner):
    session = SSHSession(connection, state).start()
    saved = state.with_name("original");state.rename(saved);state.mkdir(mode=0o700)
    before = len(runner.calls)
    assert not session.check()
    session.close()
    assert len(runner.calls) == before and (saved / "s").exists()


def test_writable_ancestor_is_rejected(connection, state, runner):
    parent = state.parent / "writable";parent.mkdir(mode=0o700);parent.chmod(0o777)
    with pytest.raises(TransportError): SSHSession(connection, parent / "private").start()
    assert runner.calls == [] and not (parent / "private").exists()


def test_nested_state_directories_are_private_even_with_shared_umask(connection, state, runner):
    nested = state / "broker" / "lab"
    previous = os.umask(0o002)
    try:
        session = SSHSession(connection, nested).start()
    finally:
        os.umask(previous)
    try:
        assert all(directory.stat().st_mode & 0o777 == 0o700 for directory in (state, state / "broker", nested))
    finally:
        session.close()


def test_existing_group_writable_parent_is_diagnosed_without_changing_it(connection, state, runner):
    state.mkdir(mode=0o700);state.chmod(0o775)
    with pytest.raises(ssh_session.SessionStorageError) as error:
        SSHSession(connection, state / "lab").start()
    assert error.value.code == "STATE_ANCESTOR_WRITABLE"
    assert state.stat().st_mode & 0o777 == 0o775
    assert not (state / "lab").exists() and not runner.calls


@pytest.mark.parametrize("lifetime", [0, 59, 86401, True, 60.0])
def test_lifetime_has_finite_validated_bounds(connection, state, lifetime):
    with pytest.raises(TransportError): SSHSession(connection, state, lifetime)


def test_expanding_or_excessive_socket_paths_are_rejected(connection, state):
    for path in ("relative", str(state / "%h"), str(state / "has space"), "/tmp/" + "x" * 100):
        with pytest.raises(TransportError): SSHSession(connection, path)


@pytest.mark.parametrize("change", ["host", "user", "port", "identity", "known_hosts", "mode"])
def test_connection_uses_transport_validation_and_requires_explicit_identity(connection, state, change):
    if change == "host": connection["ssh"]["host"] = "-oProxyCommand=bad"
    elif change == "user": connection["ssh"]["user"] = "bad user"
    elif change == "port": connection["ssh"]["port"] = True
    elif change == "identity": connection["ssh"].pop("identity_file")
    elif change == "known_hosts": connection["ssh"]["known_hosts"] = "/tmp/known %h"
    else: connection["ssh"]["mode"] = "unsupported"
    with pytest.raises(TransportError): SSHSession(connection, state)


def test_control_failure_and_repeated_close_are_safe(connection, state, runner):
    session = SSHSession(connection, state)
    assert not session.check()
    session.close();assert runner.calls == []
    session.start()
    runner.control_error = subprocess.TimeoutExpired("ssh", 10)
    assert not session.check()
    session.close()
    count = len(runner.calls);session.close()
    assert len(runner.calls) == count


def test_repeated_start_reuses_only_the_master_this_object_created(connection, state, runner):
    session = SSHSession(connection, state).start()
    assert session.start() is session
    assert sum("-M" in command for command, _ in runner.calls) == 1
    session.close()
