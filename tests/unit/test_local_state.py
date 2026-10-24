"""Private local setup, explicit configuration and process ownership."""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from compute_relay.local_runtime import configure, register_options, run, service_environment
from compute_relay.local_state import AlreadyRunning, LocalState, LocalStateError, default_directory, validate_installation


def arguments(*items):
    parser = argparse.ArgumentParser()
    register_options(parser)
    return parser.parse_args(items)


def installation():
    return {"schema_version": "1.0", "installation_id": str(uuid4()), "owner_id": str(uuid4()),
        "portal_auth": "none", "port": 8768, "extensions": ["storage", "environments", "workflows"], "initialized": True}


def test_default_state_uses_xdg_data_and_has_no_working_directory_dependency(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_directory() == tmp_path / "compute-relay"
    monkeypatch.setenv("XDG_DATA_HOME", "relative-data")
    assert default_directory() == Path.home() / ".local/share/compute-relay"


def test_metadata_and_lock_are_private_and_only_one_launcher_owns_state(tmp_path):
    path = tmp_path / "state"
    with LocalState(path) as state, state.lock():
        state.write("installation.json", installation())
        assert state.read("installation.json")["portal_auth"] == "none"
        with LocalState(path) as other, pytest.raises(AlreadyRunning), other.lock():
            pytest.fail("Another launcher obtained the same installation lock")
    assert path.stat().st_mode & 0o777 == 0o700
    assert (path / "installation.json").stat().st_mode & 0o777 == 0o600
    assert (path / "runtime.lock").stat().st_mode & 0o777 == 0o600
    with LocalState(path) as state, state.lock():
        pass


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public"])
def test_metadata_refuses_untrusted_existing_files_without_replacing_them(tmp_path, kind):
    original = tmp_path / "original"
    original.write_text(json.dumps(installation()));original.chmod(0o600)
    with LocalState(tmp_path / "state") as state:
        target = state.directory / "installation.json"
        if kind == "symlink": target.symlink_to(original)
        elif kind == "hardlink": os.link(original, target)
        else: target.write_text(original.read_text());target.chmod(0o644)
        before = original.read_bytes()
        with pytest.raises(LocalStateError): state.write("installation.json", installation())
        assert original.read_bytes() == before


@pytest.mark.parametrize("mutation", [
    {"schema_version": "old"}, {"installation_id": "invalid"}, {"owner_id": "invalid"},
    {"portal_auth": "automatic"}, {"port": 0}, {"port": True}, {"initialized": 1},
    {"extensions": ["storage", "storage"]}, {"extensions": ["untrusted.module"]},
    {"unexpected": "setting"},
])
def test_installation_has_one_current_explicit_schema(mutation):
    with pytest.raises(LocalStateError): validate_installation({**installation(), **mutation})


def test_passwordless_is_explicit_on_first_launch_and_persists_without_reconfiguration(tmp_path):
    with LocalState(tmp_path / "state") as state:
        with pytest.raises(LocalStateError, match="First launch"):
            configure(state, arguments("--non-interactive"))
        config, boot = configure(state, arguments("--no-password", "--port", "18876"))
        assert config["portal_auth"] == "none" and config["port"] == 18876
        assert boot == {"set_password": True, "password": None, "initialized": False}
        config["initialized"] = True;state.write("installation.json", config)
        reused, boot = configure(state, arguments("--non-interactive"))
        assert reused == config and boot["set_password"] is False
        assert configure(state, arguments("--no-password"))[1]["set_password"] is False
        with pytest.raises(LocalStateError, match="saved port"):
            configure(state, arguments("--port", "18877"))


def test_first_interactive_launch_can_choose_blank_password(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    prompts = []
    monkeypatch.setattr("getpass.getpass", lambda prompt: prompts.append(prompt) or "")
    with LocalState(tmp_path / "state") as state:
        config, boot = configure(state, arguments())
    assert config["portal_auth"] == "none" and boot["password"] is None
    assert len(prompts) == 1


def test_private_password_file_is_not_copied_into_metadata(tmp_path):
    password = tmp_path / "password"
    password.write_text("synthetic-portal-password\n");password.chmod(0o600)
    with LocalState(tmp_path / "state") as state:
        config, boot = configure(state, arguments("--owner-password-file", str(password)))
        assert config["portal_auth"] == "password"
        assert boot["password"] == "synthetic-portal-password"
        assert "synthetic-portal-password" not in json.dumps(config)
        password.chmod(0o644)
        with pytest.raises(LocalStateError, match="private regular"):
            configure(state, arguments("--owner-password-file", str(password)))


def test_unknown_existing_database_is_not_adopted(tmp_path):
    with LocalState(tmp_path / "state") as state:
        database = state.directory / "control.sqlite3"
        database.write_bytes(b"unrecognized-installation")
        with pytest.raises(LocalStateError, match="without installation metadata"):
            configure(state, arguments("--no-password"))
        assert database.read_bytes() == b"unrecognized-installation"


def test_second_launch_during_initial_setup_does_not_suggest_corrupt_metadata(tmp_path, capsys):
    path = tmp_path / "state"
    with LocalState(path) as state, state.lock(), pytest.raises(SystemExit):
        run(arguments("--data-directory", str(path), "--no-password", "--no-browser"))
    error = capsys.readouterr().err
    assert "already starting" in error and "invalid" not in error


def test_service_environment_cannot_inherit_another_database_or_installation(monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER_DB_ENGINE", "postgresql")
    monkeypatch.setenv("BROKER_DB_PASSWORD_FILE", "/unrelated/private/password")
    monkeypatch.setenv("BROKER_PROJECT_TOKEN", "synthetic-secret")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "untrusted.settings")
    config = installation();original = deepcopy(config)
    with LocalState(tmp_path / "state") as state:
        environment = service_environment(state, config, str(uuid4()))
        assert environment["BROKER_DATA_DIR"] == str(state.directory)
        assert environment["BROKER_DB_PATH"] == str(state.directory / "control.sqlite3")
    assert environment["BROKER_DB_ENGINE"] == "sqlite"
    assert "BROKER_DB_PASSWORD_FILE" not in environment and "BROKER_PROJECT_TOKEN" not in environment
    assert "DJANGO_SETTINGS_MODULE" not in environment
    assert environment["BROKER_ALLOWED_HOSTS"] == "127.0.0.1" and config == original
