"""Named connector shortcuts never load keys, state contents or start hidden SSH."""

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from compute_broker import connector_profiles as profiles


@pytest.fixture
def registry(tmp_path):
    return profiles.ConnectorProfiles(tmp_path / "shortcuts")


@pytest.fixture
def state(tmp_path):
    # Deliberately not JSON: the registry must not read private connector state.
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    (directory / "state.json").write_text("synthetic-private-worker-token\n")
    (directory / "state.json").chmod(0o600)
    return directory


def test_pristine_list_is_read_only(registry):
    assert registry.list() == []
    assert not registry.directory.exists()


def test_add_list_and_repeat_only_store_name_and_state_path(registry, state):
    registered = registry.add("agmn", state)
    assert registry.list() == [registered]
    assert registry.add("agmn", state) == registered
    document = json.loads((registry.directory / "connectors.json").read_text())
    assert document == {"schema_version": "1.0", "connectors": [{"name": "agmn", "state_directory": str(state)}]}
    assert "synthetic-private-worker-token" not in json.dumps(document)
    assert registry.directory.stat().st_mode & 0o777 == 0o700
    for path in registry.directory.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600


def test_relative_state_path_is_resolved_without_reading_state(registry, state, monkeypatch):
    monkeypatch.chdir(state.parent)
    assert registry.add("agmn", "state").state_directory == str(state)


def test_registry_uses_xdg_config_and_home_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert profiles.ConnectorProfiles().directory == tmp_path / "config/compute-broker"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    assert profiles.ConnectorProfiles().directory == tmp_path / "home/.config/compute-broker"


@pytest.mark.parametrize("name", ["", ".", "../agmn", "agmn\nnew line", "agmn label", "a" * 65, "-option"])
def test_invalid_names_do_not_create_registry(registry, state, name):
    with pytest.raises(profiles.ConnectorProfileError, match="shortcut name"):
        registry.add(name, state)
    assert not registry.directory.exists()


def test_duplicate_name_or_directory_needs_explicit_removal(registry, state, tmp_path):
    registry.add("agmn", state)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    (other / "state.json").write_text("another-private-token")
    (other / "state.json").chmod(0o600)
    with pytest.raises(profiles.ConnectorProfileError, match="already registered"):
        registry.add("agmn", other)
    with pytest.raises(profiles.ConnectorProfileError, match="already has the shortcut 'agmn'"):
        registry.add("lab", state)
    assert registry.list() == [profiles.ConnectorProfile("agmn", str(state))]


def test_remove_only_forgets_shortcut_and_preserves_state(registry, state):
    original = (state / "state.json").read_bytes()
    registry.add("agmn", state)
    registry.remove("agmn")
    assert registry.list() == []
    assert (state / "state.json").read_bytes() == original
    with pytest.raises(profiles.ConnectorProfileError, match="No saved connector"):
        registry.remove("agmn")


def test_missing_unpaired_and_removed_state_have_actionable_errors(registry, state, tmp_path):
    unpaired = tmp_path / "unpaired"
    unpaired.mkdir(mode=0o700)
    with pytest.raises(profiles.ConnectorProfileError, match="first pairing"):
        registry.add("unpaired", unpaired)
    registry.add("agmn", state)
    (state / "state.json").unlink()
    with pytest.raises(profiles.ConnectorProfileError, match="first pairing"):
        registry.get("agmn")
    with pytest.raises(profiles.ConnectorProfileError, match="No saved connector"):
        registry.get("other")


@pytest.mark.parametrize("mutation", ["symlink-directory", "symlink-file", "hardlink-file", "public-file", "public-directory", "writable-parent"])
def test_unsafe_registry_paths_are_rejected(registry, state, tmp_path, mutation):
    registry.add("agmn", state)
    registry_file = registry.directory / "connectors.json"
    if mutation == "symlink-directory":
        moved = tmp_path / "moved"
        registry.directory.rename(moved)
        registry.directory.symlink_to(moved, target_is_directory=True)
    elif mutation == "symlink-file":
        moved = tmp_path / "original.json"
        registry_file.rename(moved)
        registry_file.symlink_to(moved)
    elif mutation == "hardlink-file":
        os.link(registry_file, tmp_path / "hardlink.json")
    elif mutation == "public-file":
        registry_file.chmod(0o644)
    elif mutation == "public-directory":
        registry.directory.chmod(0o755)
    elif mutation == "writable-parent":
        tmp_path.chmod(0o777)
    try:
        with pytest.raises(profiles.ConnectorProfileError):
            registry.list()
    finally:
        tmp_path.chmod(0o700)


@pytest.mark.parametrize("mutation", ["symlink-directory", "symlink-file", "hardlink-file", "public-file", "public-directory"])
def test_unsafe_connector_state_is_not_registered(registry, state, tmp_path, mutation):
    filename = state / "state.json"
    if mutation == "symlink-directory":
        alias = tmp_path / "alias"
        alias.symlink_to(state, target_is_directory=True)
        state = alias
    elif mutation == "symlink-file":
        other = tmp_path / "other.json"
        filename.rename(other)
        filename.symlink_to(other)
    elif mutation == "hardlink-file":
        os.link(filename, tmp_path / "hardlink.json")
    elif mutation == "public-file":
        filename.chmod(0o644)
    elif mutation == "public-directory":
        state.chmod(0o755)
    with pytest.raises(profiles.ConnectorProfileError):
        registry.add("agmn", state)
    assert not registry.directory.exists()


def test_unsafe_lock_is_not_followed(registry, state, tmp_path):
    registry.add("agmn", state)
    (registry.directory / "connectors.lock").unlink()
    target = tmp_path / "target"
    target.write_text("preserve-me")
    (registry.directory / "connectors.lock").symlink_to(target)
    with pytest.raises(profiles.ConnectorProfileError):
        registry.remove("agmn")
    assert target.read_text() == "preserve-me"


@pytest.mark.parametrize("content", [
    "not json", json.dumps({"schema_version": "1.0", "connectors": [], "secret": "synthetic-do-not-print"}),
    json.dumps({"schema_version": "1.0", "connectors": [{"name": "agmn", "state_directory": "/tmp/state", "token": "synthetic-do-not-print"}]}),
    "x" * (64 * 1024 + 1),
])
def test_corrupt_registry_diagnostic_does_not_echo_contents(registry, state, content):
    registry.add("agmn", state)
    (registry.directory / "connectors.json").write_text(content)
    with pytest.raises(profiles.ConnectorProfileError, match="registry is invalid") as caught:
        registry.list()
    assert "synthetic-do-not-print" not in str(caught.value)


def parser():
    result = argparse.ArgumentParser()
    profiles.register_commands(result.add_subparsers(required=True))
    return result


def test_start_invokes_connector_in_same_process_with_explicit_label(registry, state, monkeypatch, capsys):
    from compute_broker.worker import personal_connector
    registry.add("Ramses", state)
    received = []
    monkeypatch.setattr(personal_connector, "main", received.append)
    args = parser().parse_args(["connectors", "--registry-directory", str(registry.directory), "start", "Ramses", "--verbose"])
    args.run(args)
    assert len(received) == 1
    assert received[0].state_directory == str(state)
    assert received[0].verbose is True and received[0].setup is None
    assert received[0].connector_label == "Ramses"
    assert received[0].identity_file is None and received[0].known_hosts is None
    assert received[0].non_interactive is False
    output = capsys.readouterr()
    assert "Starting connector 'Ramses' in this terminal" in output.out
    assert "MFA" in output.out and "synthetic-private-worker-token" not in output.out + output.err


def test_start_all_delegates_one_ordered_foreground_runner(registry, state, tmp_path, monkeypatch, capsys):
    from compute_broker.worker import personal_connector
    second = tmp_path / "second"
    second.mkdir(mode=0o700)
    (second / "state.json").write_text("synthetic-second-token")
    (second / "state.json").chmod(0o600)
    registry.add("Ramses", second)
    registry.add("agmn", state)
    calls = []
    monkeypatch.setattr(personal_connector, "main", lambda _: pytest.fail("All-start called a blocking single runner"))
    monkeypatch.setattr(personal_connector, "main_many", calls.append, raising=False)
    args = parser().parse_args(["connectors", "--registry-directory", str(registry.directory), "start", "--all", "-v"])
    args.run(args)
    assert len(calls) == 1
    assert [entry.connector_label for entry in calls[0]] == ["agmn", "Ramses"]
    assert [entry.state_directory for entry in calls[0]] == [str(state), str(second)]
    assert all(entry.verbose and not entry.non_interactive for entry in calls[0])
    output = capsys.readouterr().out
    assert "one cluster at a time" in output and "agmn" in output and "Ramses" in output
    assert "synthetic-second-token" not in output


def test_start_all_validates_every_state_before_authentication(registry, state, tmp_path, monkeypatch, capsys):
    from compute_broker.worker import personal_connector
    registry.add("agmn", state)
    (state / "state.json").unlink()
    monkeypatch.setattr(personal_connector, "main_many", lambda _: pytest.fail("Invalid state opened SSH"), raising=False)
    args = parser().parse_args(["connectors", "--registry-directory", str(registry.directory), "start", "--all"])
    with pytest.raises(SystemExit):
        args.run(args)
    assert "first pairing" in capsys.readouterr().err


def test_start_all_requires_saved_shortcuts(registry, capsys):
    args = parser().parse_args(["connectors", "--registry-directory", str(registry.directory), "start", "--all"])
    with pytest.raises(SystemExit):
        args.run(args)
    assert "No connector shortcuts yet" in capsys.readouterr().err


def test_start_name_and_all_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parser().parse_args(["connectors", "start", "agmn", "--all"])


def test_interactive_selection_shows_names_before_ssh(registry, state, monkeypatch, capsys):
    registry.add("agmn", state)
    monkeypatch.setattr(profiles.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    assert profiles._selected_profile(registry, None).name == "agmn"
    output = capsys.readouterr().out
    assert "agmn" in output and str(state) in output


def test_no_unnamed_start_in_noninteractive_terminal(registry, state, monkeypatch):
    registry.add("agmn", state)
    monkeypatch.setattr(profiles.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(profiles.ConnectorProfileError, match="Specify the connector name"):
        profiles._selected_profile(registry, None)


def test_cli_add_list_remove_and_errors_are_safe(registry, state, capsys):
    command = parser()
    prefix = ["connectors", "--registry-directory", str(registry.directory)]
    for operation in [["add", "agmn", "--state-directory", str(state)], ["list"], ["remove", "agmn"]]:
        args = command.parse_args([*prefix, *operation])
        args.run(args)
    output = capsys.readouterr().out
    assert "compute-broker connectors start agmn" in output
    assert "agmn\t" + str(state) in output
    assert "state and cluster configuration are preserved" in output
    args = command.parse_args([*prefix, "start", "agmn"])
    with pytest.raises(SystemExit) as caught:
        args.run(args)
    assert caught.value.code == 1
    assert "No saved connector" in capsys.readouterr().err
