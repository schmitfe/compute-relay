"""Personal connector lifecycle without network, native SSH or secret output."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
import signal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from compute_broker.worker import async_client, async_runner, personal_connector as connector, ssh_session
from compute_broker.worker.client import ControlError
from compute_broker.worker.enrollment import profile_metadata


def test_empty_key_hint_can_choose_locally_without_changing_download(setup, tmp_path, monkeypatch):
    from compute_broker.worker import file_picker
    setup["local"].pop("identity_file")
    monkeypatch.setattr(file_picker, "choose_identity_file", lambda: "/local/picked-private-key")
    state = connector.prepare(setup, tmp_path, allow_picker=True)
    assert state["connection"]["ssh"]["identity_file"] == "/local/picked-private-key"
    assert "identity_file" not in setup["local"]


def test_cancelled_picker_exits_before_login_or_pairing(setup, tmp_path, monkeypatch, sessions, capsys):
    from compute_broker.worker import file_picker
    def cancel():
        raise file_picker.FileSelectionCancelled()
    monkeypatch.setattr(file_picker, "choose_identity_file", cancel)
    args = arguments(tmp_path, setup, choose_identity=True)
    args.non_interactive = False
    connector.main(args)
    assert not sessions and not (tmp_path / "state/state.json").exists()
    output = capsys.readouterr()
    assert "cancelled" in output.out and setup["pairing_token"] not in output.out + output.err


def test_noninteractive_start_never_opens_requested_picker(setup, tmp_path, monkeypatch, sessions):
    from compute_broker.worker import file_picker
    monkeypatch.setattr(file_picker, "choose_identity_file", lambda: pytest.fail("Noninteractive startup opened a picker"))
    with pytest.raises(SystemExit):
        connector.main(arguments(tmp_path, setup, choose_identity=True))
    assert not sessions


def test_writable_parent_is_reported_before_picker_login_or_pairing(setup, tmp_path, monkeypatch, sessions, pairing, capsys):
    from compute_broker.worker import file_picker
    parent = tmp_path / "broker";parent.mkdir(mode=0o775);parent.chmod(0o775)
    args = arguments(tmp_path, setup, choose_identity=True)
    args.state_directory = str(parent / "lab")
    args.non_interactive = False
    monkeypatch.setattr(file_picker, "choose_identity_file", lambda: pytest.fail("Unsafe state opened a picker"))
    with pytest.raises(SystemExit):
        connector.main(args)
    diagnostic = capsys.readouterr().err
    assert "STATE_ANCESTOR_WRITABLE" in diagnostic and "group" in diagnostic
    assert setup["pairing_token"] not in diagnostic and not sessions and not pairing.calls
    assert parent.stat().st_mode & 0o777 == 0o775 and not (parent / "lab").exists()


def test_connector_creates_every_missing_state_level_privately(setup, tmp_path, sessions, pairing, served):
    args = arguments(tmp_path, setup)
    args.state_directory = str(tmp_path / "broker" / "lab")
    previous = os.umask(0o002)
    try:
        connector.run(args)
    finally:
        os.umask(previous)
    assert (tmp_path / "broker").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "broker/lab").stat().st_mode & 0o777 == 0o700
    assert len(pairing.calls) == 1


def test_expired_setup_has_actionable_safe_cli_diagnostic(setup, tmp_path, sessions, pairing, capsys):
    setup["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with pytest.raises(SystemExit):
        connector.main(arguments(tmp_path, setup))
    diagnostic = capsys.readouterr().err
    assert "SETUP_EXPIRED" in diagnostic and "download a fresh" in diagnostic
    assert setup["pairing_token"] not in diagnostic and not sessions and not pairing.calls


@pytest.fixture
def setup():
    return {"schema_version": "1.0", "broker_url": "https://broker.example.invalid",
        "registration_id": str(uuid4()), "owner_id": str(uuid4()), "name": "Synthetic cluster",
        "pairing_token": "cbpair_synthetic_private_pairing_token", "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "target": {"host": "cluster.example.invalid", "user": "synthetic", "port": 2222},
        "remote_root": "/srv/broker/jobs", "local": {"identity_file": "/local/reviewed-key", "session_seconds": 60},
        "site_profile": {"automatic_execution_allowed": False}}


def arguments(tmp_path, setup=None, **changes):
    filename = tmp_path / "setup.json"
    if setup is not None:
        connector.write_private(filename, setup)
    return SimpleNamespace(setup=str(filename) if setup else None, state_directory=str(tmp_path / "state"),
        identity_file=None, known_hosts=None, allow_insecure_http=False, non_interactive=True, once=True, **changes)


@pytest.fixture
def sessions(monkeypatch):
    created = []
    class Session:
        def __init__(self, connection, directory, lifetime_seconds):
            self.connection, self.directory, self.lifetime = deepcopy(connection), directory, lifetime_seconds
            self.started, self.closed, self.checks = [], 0, 0
            created.append(self)
        def start(self, interactive):
            self.started.append(interactive)
            return self
        def runtime_configuration(self):
            result = deepcopy(self.connection)
            result["ssh_path"] = "/usr/bin/ssh"
            result["ssh"].update(mode="mfa", control_path=str(self.directory / "s"), config_file="/dev/null")
            result["ssh"].pop("identity_file", None)
            return result
        def check(self):
            self.checks += 1
            return True
        def close(self):
            self.closed += 1
    monkeypatch.setattr(ssh_session, "SSHSession", Session)
    return created


@pytest.fixture
def pairing(monkeypatch, setup):
    calls = []
    reply = {"owner_id": setup["owner_id"], "worker_id": str(uuid4()), "connection_id": str(uuid4()),
        "worker_token": "cbw_synthetic_private_worker_token"}
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def request(self, method, path, body):
            calls.append((method, path, deepcopy(body)))
            return deepcopy(reply)
    original = connector.pair
    monkeypatch.setattr(connector, "pair", lambda *args, **kwargs: original(*args, client_factory=Client, **kwargs))
    return SimpleNamespace(calls=calls, reply=reply)


@pytest.fixture
def served(monkeypatch):
    calls = []
    async def serve(state, session, directory, *, once, deadline):
        calls.append((deepcopy(state), session, directory, once, deadline))
    monkeypatch.setattr(connector, "serve", serve)
    return calls


def test_pair_then_resume_preserves_local_runtime_and_profile_fingerprint(tmp_path, setup, sessions, pairing, served, capsys):
    args = arguments(tmp_path, setup)
    connector.main(args)
    directory = tmp_path / "state"
    persisted = connector.read_private(directory / "state.json")
    assert persisted["worker_token"] == pairing.reply["worker_token"]
    assert "pairing_token" not in persisted and persisted["connection"]["ssh"]["identity_file"] == "/local/reviewed-key"
    assert (directory / "state.json").stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert not (directory / "pairing-pending.json").exists()
    first_alias, first_profile = connector.runtime_profile(persisted, sessions[0])
    args.setup = None
    connector.main(args)
    second_alias, second_profile = connector.runtime_profile(persisted, sessions[1])
    assert profile_metadata(first_alias, first_profile) == profile_metadata(second_alias, second_profile)
    assert len(pairing.calls) == 1 and len(served) == 2
    assert sessions[0].closed == sessions[1].closed == 1
    assert sessions[0].started == sessions[1].started == [False]
    output = capsys.readouterr()
    assert setup["pairing_token"] not in output.out + output.err
    assert pairing.reply["worker_token"] not in output.out + output.err
    sent = json.dumps(pairing.calls[0][2])
    assert "identity_file" not in sent and "control_path" not in sent and "/local/reviewed-key" not in sent


def test_pairing_response_cannot_choose_key_or_replace_frozen_runtime(tmp_path, setup, sessions, pairing, served):
    pairing.reply.update(connection={"ssh": {"identity_file": "/server/chosen-key"}},
        broker_url="https://other.example.invalid", session_seconds=86400, site_profile={"automatic_execution_allowed": True})
    args = arguments(tmp_path, setup)
    args.identity_file = "/human/explicit-key"
    connector.main(args)
    saved = connector.read_private(tmp_path / "state" / "state.json")
    assert saved["connection"]["ssh"]["identity_file"] == "/human/explicit-key"
    assert saved["broker_url"] == setup["broker_url"] and saved["session_seconds"] == 60
    assert saved["site_profile"] == {"automatic_execution_allowed": False}
    assert sessions[0].closed == 1


def test_failed_pairing_keeps_uncertainty_fence_and_closes_session(tmp_path, setup, sessions, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise ControlError("synthetic failure containing " + setup["pairing_token"])
    monkeypatch.setattr(connector, "pair", fail)
    args = arguments(tmp_path, setup)
    with pytest.raises(SystemExit) as error:
        connector.main(args)
    assert error.value.code == 1 and sessions[0].closed == 1
    directory = tmp_path / "state"
    assert not (directory / "state.json").exists()
    assert connector.read_private(directory / "pairing-pending.json") == {"registration_id": setup["registration_id"]}
    with pytest.raises(ValueError, match="uncertain"):
        connector.run(args)
    assert len(sessions) == 1
    output = capsys.readouterr()
    assert setup["pairing_token"] not in output.out + output.err


def test_wrong_owner_pair_reply_never_writes_worker_secret(tmp_path, setup, sessions, pairing, served):
    pairing.reply["owner_id"] = str(uuid4())
    with pytest.raises(ValueError, match="different owner"):
        connector.run(arguments(tmp_path, setup))
    assert not (tmp_path / "state" / "state.json").exists() and sessions[0].closed == 1
    assert not served


def test_expired_setup_is_rejected_before_any_ssh_login(tmp_path, setup, sessions, pairing):
    setup["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="expired"):
        connector.run(arguments(tmp_path, setup))
    assert not sessions and not pairing.calls


def test_paired_connector_rejects_local_key_override_on_resume(tmp_path, setup, sessions, pairing, served):
    args = arguments(tmp_path, setup);connector.run(args)
    args.setup = None;args.identity_file = "/different/local-key"
    with pytest.raises(ValueError, match="fresh setup"):
        connector.run(args)
    assert len(sessions) == 1 and len(pairing.calls) == 1


def test_existing_paired_directory_does_not_consume_new_setup(tmp_path, setup, sessions, pairing, served):
    args = arguments(tmp_path, setup);connector.run(args)
    with pytest.raises(ValueError, match="already paired"):
        connector.run(args)
    assert len(pairing.calls) == 1 and len(sessions) == 1


def test_process_lock_prevents_two_connectors_using_one_state(tmp_path, setup, sessions, pairing):
    args = arguments(tmp_path, setup)
    directory = tmp_path / "state";directory.mkdir(mode=0o700)
    descriptor = os.open(directory / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError): connector.run(args)
    finally:
        os.close(descriptor)
    assert not sessions and not pairing.calls


@pytest.mark.parametrize("case", ["permissions", "symlink", "hardlink"])
def test_private_setup_file_cannot_be_public_or_aliased(tmp_path, setup, sessions, pairing, case):
    args = arguments(tmp_path, setup)
    filename = tmp_path / "setup.json"
    if case == "permissions": filename.chmod(0o644)
    elif case == "symlink":
        destination = tmp_path / "original.json";filename.rename(destination);filename.symlink_to(destination)
    else: os.link(filename, tmp_path / "duplicate.json")
    with pytest.raises((ValueError, OSError)): connector.run(args)
    assert not sessions and not pairing.calls


def patch_async_runtime(monkeypatch, *, poll):
    closed = []
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def close(self):
            closed.append(True)
    class Worker:
        def __init__(self, *args, **kwargs):
            pass
        async def poll_once(self):
            return await poll()
    monkeypatch.setattr(async_client, "AsyncJsonClient", Client)
    monkeypatch.setattr(async_runner, "AsyncWorker", Worker)
    return closed


def test_session_deadline_closes_http_client_and_ssh_master(tmp_path, setup, sessions, pairing, monkeypatch, capsys):
    async def blocked_poll():
        await asyncio.Event().wait()
    closed = patch_async_runtime(monkeypatch, poll=blocked_poll)
    ticks = iter([100, 161])
    monkeypatch.setattr(connector, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    connector.main(arguments(tmp_path, setup))
    assert closed == [True] and sessions[0].closed == 1
    output = capsys.readouterr()
    assert "lifetime reached" in output.err
    assert pairing.reply["worker_token"] not in output.out + output.err


@pytest.mark.parametrize("status", [401, 403])
def test_revoked_connector_closes_client_and_master(tmp_path, setup, sessions, pairing, monkeypatch, status):
    async def withdrawn():
        raise ControlError("CONTROL_HTTP_ERROR", status)
    closed = patch_async_runtime(monkeypatch, poll=withdrawn)
    connector.main(arguments(tmp_path, setup))
    assert closed == [True] and sessions[0].closed == 1


def test_sigterm_closes_master_and_restores_previous_handler(tmp_path, setup, sessions, pairing, monkeypatch, capsys):
    previous = signal.getsignal(signal.SIGTERM)
    async def terminated(*args, **kwargs):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    monkeypatch.setattr(connector, "serve", terminated)
    connector.main(arguments(tmp_path, setup))
    assert sessions[0].closed == 1 and signal.getsignal(signal.SIGTERM) is previous
    output = capsys.readouterr()
    assert "Connector stopped" in output.out
    assert setup["pairing_token"] not in output.out + output.err
    assert pairing.reply["worker_token"] not in output.out + output.err
