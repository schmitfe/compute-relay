"""Mocked local dialogs only: no native GUI, real SSH identity, or key read."""

import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
from unittest.mock import Mock

import pytest

from compute_broker.worker import file_picker
from compute_broker.worker.file_picker import (
    FileSelectionCancelled, FileSelectionUnavailable, choose_identity_file,
)


@pytest.fixture
def native(monkeypatch, tmp_path):
    selected = tmp_path / "synthetic identity with spaces"
    selected.touch(mode=0o600)
    root = Mock()
    dialog = Mock(return_value=str(selected))
    toolkit = SimpleNamespace(Tk=Mock(return_value=root),
        filedialog=SimpleNamespace(askopenfilename=dialog))
    monkeypatch.setitem(sys.modules, "tkinter", toolkit)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(file_picker, "_zenity_selection", Mock(side_effect=AssertionError("unexpected fallback")))
    return SimpleNamespace(selected=selected, root=root, dialog=dialog, toolkit=toolkit, home=tmp_path)


def test_tk_returns_full_path_only_and_destroys_window(native, monkeypatch, capsys):
    monkeypatch.setattr(Path, "open", Mock(side_effect=AssertionError("picker must not read a key")))
    monkeypatch.setattr("builtins.open", Mock(side_effect=AssertionError("picker must not read a key")))
    assert choose_identity_file() == str(native.selected)
    native.dialog.assert_called_once_with(parent=native.root, title="Select your SSH private key",
        initialdir=str(native.home / ".ssh"))
    native.root.withdraw.assert_called_once_with()
    native.root.destroy.assert_called_once_with()
    assert capsys.readouterr() == ("", "")


def test_tk_cancel_is_fixed_and_does_not_start_another_picker(native):
    native.dialog.return_value = ""
    with pytest.raises(FileSelectionCancelled, match="^SSH key selection was cancelled\\.$"):
        choose_identity_file()
    native.root.destroy.assert_called_once_with()


@pytest.mark.parametrize("kind", ["directory", "missing", "relative", "newline", "nul", "not-string"])
def test_native_selection_must_be_an_existing_absolute_file(native, kind):
    native.dialog.return_value = {"directory": str(native.home), "missing": str(native.home / "missing"),
        "relative": "identity", "newline": str(native.selected) + "\n", "nul": str(native.selected) + "\x00",
        "not-string": None}[kind]
    with pytest.raises(FileSelectionUnavailable):
        choose_identity_file()
    native.root.destroy.assert_called_once_with()


def test_toolkit_failure_destroys_created_window_before_fallback(native, monkeypatch):
    native.dialog.side_effect = RuntimeError("private/path must not become an error")
    fallback = Mock(return_value=str(native.selected))
    monkeypatch.setattr(file_picker, "_zenity_selection", fallback)
    assert choose_identity_file() == str(native.selected)
    native.root.destroy.assert_called_once_with()
    fallback.assert_called_once_with()


def test_tk_construction_failure_can_fall_back(native, monkeypatch):
    native.toolkit.Tk.side_effect = RuntimeError("headless")
    monkeypatch.setattr(file_picker, "_zenity_selection", lambda: str(native.selected))
    assert choose_identity_file() == str(native.selected)
    native.root.destroy.assert_not_called()


def test_window_cleanup_error_does_not_expose_native_details(native):
    native.root.destroy.side_effect = RuntimeError("private-path")
    assert choose_identity_file() == str(native.selected)


@pytest.fixture
def fallback(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "tkinter", None)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(os, "environ", {"DISPLAY": ":99", "PATH": "/untrusted/bin",
        "WAYLAND_DISPLAY": "wayland-9", "XAUTHORITY": "/run/private/xauth", "XDG_RUNTIME_DIR": "/run/user/test",
        "BROKER_PROJECT_TOKEN": "synthetic-token-must-not-propagate", "SSH_AUTH_SOCK": "/private/agent",
        "LD_PRELOAD": "/untrusted/library", "PYTHONPATH": "/untrusted/python",
        "DBUS_STARTER_ADDRESS": "untrusted-starter", "GTK_MODULES": "untrusted-module"})
    selected = tmp_path / "synthetic identity with spaces"
    selected.touch(mode=0o600)
    which = Mock(return_value="/usr/bin/zenity")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=str(selected) + "\n"))
    monkeypatch.setattr(file_picker.shutil, "which", which)
    monkeypatch.setattr(file_picker.subprocess, "run", run)
    return SimpleNamespace(selected=selected, which=which, run=run, home=tmp_path)


def test_linux_fallback_uses_fixed_executable_arguments_and_secret_free_gui_env(fallback, capsys):
    assert choose_identity_file() == str(fallback.selected)
    fallback.which.assert_called_once_with("zenity", path="/usr/bin:/bin")
    args, options = fallback.run.call_args
    assert args == (["/usr/bin/zenity", "--file-selection", "--title=Select your SSH private key",
        "--filename=" + str(fallback.home / ".ssh") + os.sep],)
    assert options["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GIO_USE_VFS": "local",
        "DISPLAY": ":99", "WAYLAND_DISPLAY": "wayland-9", "XAUTHORITY": "/run/private/xauth",
        "XDG_RUNTIME_DIR": "/run/user/test"}
    assert options["shell"] is False and options["timeout"] == 300
    assert options["stdin"] == subprocess.DEVNULL and options["stderr"] == subprocess.DEVNULL
    assert options["stdout"] == subprocess.PIPE and options["close_fds"] is True
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("platform, display", [("linux", False), ("darwin", True), ("win32", True)])
def test_missing_tk_has_fixed_failure_without_supported_graphical_fallback(fallback, monkeypatch, platform, display):
    monkeypatch.setattr(sys, "platform", platform)
    if not display:
        monkeypatch.setattr(os, "environ", {})
    with pytest.raises(FileSelectionUnavailable) as caught:
        choose_identity_file()
    assert str(caught.value) == "A local file picker is unavailable; supply --identity-file with an existing private key path."
    fallback.run.assert_not_called()


@pytest.mark.parametrize("binary", [None, "/home/person/bin/zenity", "zenity", "/usr/local/bin/zenity"])
def test_gui_fallback_refuses_non_system_executable(fallback, binary):
    fallback.which.return_value = binary
    with pytest.raises(FileSelectionUnavailable):
        choose_identity_file()
    fallback.run.assert_not_called()


def test_executable_lookup_failure_is_fixed_and_does_not_run_a_process(fallback):
    fallback.which.side_effect = OSError("private-path")
    with pytest.raises(FileSelectionUnavailable) as caught:
        choose_identity_file()
    assert "private-path" not in str(caught.value)
    fallback.run.assert_not_called()


def test_zenity_cancel_never_leaks_stdout_path(fallback, capsys):
    fallback.run.return_value = SimpleNamespace(returncode=1, stdout="private-path")
    with pytest.raises(FileSelectionCancelled) as caught:
        choose_identity_file()
    assert "private-path" not in str(caught.value)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure", [OSError("private-path"), subprocess.TimeoutExpired("private-command", 300),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private-path")])
def test_fallback_errors_are_fixed_without_native_diagnostics(fallback, failure, capsys):
    fallback.run.side_effect = failure
    with pytest.raises(FileSelectionUnavailable) as caught:
        choose_identity_file()
    assert "private-path" not in str(caught.value)
    assert capsys.readouterr() == ("", "")


def test_zenity_failure_code_does_not_report_diagnostics(fallback):
    fallback.run.return_value = SimpleNamespace(returncode=2, stdout="private-path")
    with pytest.raises(FileSelectionUnavailable):
        choose_identity_file()


def test_zenity_cannot_return_multiple_paths(fallback):
    fallback.run.return_value = SimpleNamespace(returncode=0, stdout=str(fallback.selected) + "\n/private/another\n")
    with pytest.raises(FileSelectionUnavailable):
        choose_identity_file()
