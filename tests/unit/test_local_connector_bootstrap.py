"""Scope fences and durable shortcut registration around actual connector bootstrap."""

from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from compute_relay import local_connectors as local
from compute_relay.connector_profiles import ConnectorProfileError, ConnectorProfiles
from compute_relay.worker import personal_connector as connector, ssh_session


@pytest.fixture
def bootstrap(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="cb-bootstrap-") as value:
        data = Path(value)
        directory = data / "state"
        directory.mkdir(mode=0o700)
        state = {"schema_version": "1.0", "registration_id": str(uuid4()), "owner_id": str(uuid4()),
            "name": "Lab cluster", "broker_url": "http://127.0.0.1:8768", "session_seconds": 60,
            "site_profile": {"automatic_execution_allowed": False},
            "connection": {"backend": "slurm", "remote_root": "/scratch/jobs", "python_path": "/usr/bin/python3",
                "ssh": {"host": "cluster.example.invalid", "user": "scientist", "port": 22,
                    "mode": "key", "identity_file": "/private/synthetic-key", "known_hosts": str(directory / "known_hosts")}}}
        sessions = []
        class Session:
            def __init__(self, *args, **kwargs):
                self.closed = False
                sessions.append(self)
            def start(self, **kwargs):
                pass
            def close(self):
                self.closed = True
        monkeypatch.setattr(ssh_session, "SSHSession", Session)
        args = SimpleNamespace(state_directory=str(directory), setup=None, non_interactive=True, choose_identity=False,
            identity_file=None, known_hosts=None, allow_insecure_http=False, verbose=False, register_connector=True,
            registry_directory=str(data / "connectors-registry"), expected_broker_url=state["broker_url"],
            expected_registration_id=state["registration_id"], expected_target={field: state["connection"]["ssh"][field]
                for field in ("host", "user", "port")})
        yield data, state, sessions, args


@pytest.mark.parametrize("mismatch", ["broker", "registration", "host", "user", "port"])
def test_saved_state_scope_is_checked_before_any_ssh_login(bootstrap, mismatch):
    data, state, sessions, args = bootstrap
    connector.write_private(Path(args.state_directory) / "state.json", state)
    if mismatch == "broker":
        args.expected_broker_url = "http://127.0.0.1:9876"
    elif mismatch == "registration":
        args.expected_registration_id = str(uuid4())
    else:
        args.expected_target[mismatch] = 2222 if mismatch == "port" else "other"
    with pytest.raises(ValueError):
        with connector.connected(args):
            pytest.fail("Mismatched state reached connector startup")
    assert sessions == []


def prepare_pair(bootstrap, monkeypatch):
    data, state, sessions, args = bootstrap
    args.setup = str(data / "downloaded-setup.json")
    connector.write_private(args.setup, {"schema_version": "1.0", "pairing_token": "synthetic-private-pairing-token"})
    monkeypatch.setattr(connector, "prepare", lambda *a, **kw: deepcopy(state))
    monkeypatch.setattr(connector, "pair", lambda *a, **kw: {**deepcopy(state), "worker_token": "synthetic-private-worker-token"})
    return data, state, sessions, args


def test_successful_pairing_is_remembered_without_storing_credentials(bootstrap, monkeypatch, capsys):
    data, state, sessions, args = prepare_pair(bootstrap, monkeypatch)
    with connector.connected(args) as connection:
        assert connection[0]["worker_token"] == "synthetic-private-worker-token"
        registered = ConnectorProfiles(args.registry_directory).list()
        assert len(registered) == 1 and registered[0].name == "Lab-cluster"
    assert sessions[0].closed
    registry_content = (Path(args.registry_directory) / "connectors.json").read_text()
    assert "synthetic-private" not in registry_content + str(capsys.readouterr())


def test_shortcut_write_failure_does_not_discard_successful_pairing(bootstrap, monkeypatch, capsys):
    data, state, sessions, args = prepare_pair(bootstrap, monkeypatch)
    def fail(*args, **kwargs):
        raise ConnectorProfileError("synthetic-secret-exception")
    monkeypatch.setattr(ConnectorProfiles, "remember", fail)
    with connector.connected(args):
        assert connector.read_private(Path(args.state_directory) / "state.json")["worker_token"] == "synthetic-private-worker-token"
    output = capsys.readouterr()
    assert "startup shortcut could not be saved" in output.err
    assert "synthetic-secret" not in output.err + output.out
    assert sessions[0].closed
