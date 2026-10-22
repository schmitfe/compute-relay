"""Local path selection only; no SSH key is opened, uploaded or logged here."""

import os
from pathlib import Path
import shutil
import subprocess
import sys


TITLE = "Select your SSH private key"
_SYSTEM_PATH = "/usr/bin:/bin"
_ZENITY_PATHS = {"/usr/bin/zenity", "/bin/zenity"}
_GUI_ENVIRONMENT = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR")


class FileSelectionCancelled(RuntimeError):
    def __init__(self):
        super().__init__("SSH key selection was cancelled.")


class FileSelectionUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("A local file picker is unavailable; supply --identity-file with an existing private key path.")


def _tk_selection():
    root = None
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        return filedialog.askopenfilename(parent=root, title=TITLE,
            initialdir=str(Path.home() / ".ssh"))
    except Exception:
        # Missing Tk and display/toolkit failures must not expose native error
        # text, local paths, or any surrounding application state.
        raise FileSelectionUnavailable() from None
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def _zenity_selection():
    if not sys.platform.startswith("linux") or not any(os.environ.get(name)
            for name in ("DISPLAY", "WAYLAND_DISPLAY")):
        raise FileSelectionUnavailable()
    # Never resolve an executable through the caller's PATH or a local checkout.
    try:
        binary = shutil.which("zenity", path=_SYSTEM_PATH)
    except OSError:
        raise FileSelectionUnavailable() from None
    if binary not in _ZENITY_PATHS:
        raise FileSelectionUnavailable()
    environment = {"PATH": _SYSTEM_PATH, "LANG": "C.UTF-8", "GIO_USE_VFS": "local"}
    environment.update({name: os.environ[name] for name in _GUI_ENVIRONMENT if os.environ.get(name)})
    try:
        result = subprocess.run([binary, "--file-selection", "--title=" + TITLE,
            "--filename=" + str(Path.home() / ".ssh") + os.sep],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment, timeout=300, check=False, shell=False, close_fds=True,
            text=True, encoding="utf-8", errors="strict")
    except Exception:
        raise FileSelectionUnavailable() from None
    if result.returncode == 1:
        raise FileSelectionCancelled()
    if result.returncode != 0:
        raise FileSelectionUnavailable()
    # Zenity appends a single line ending. Embedded control characters remain
    # invalid rather than becoming a second argument or another selected path.
    return result.stdout.removesuffix("\n").removesuffix("\r")


def _existing_path(selection):
    if selection == "":
        raise FileSelectionCancelled()
    if (not isinstance(selection, str) or len(selection) > 4096
            or any(character in selection for character in "\r\n\x00")):
        raise FileSelectionUnavailable()
    try:
        path = Path(selection)
        if not path.is_absolute() or not path.is_file():
            raise FileSelectionUnavailable()
    except (OSError, ValueError):
        raise FileSelectionUnavailable() from None
    return str(path)


def choose_identity_file() -> str:
    """Return an existing local file's full path, without reading its contents.

    Explicit cancellation never opens another picker. A Linux Zenity fallback
    is available only when Tk fails and a graphical session is present. The
    connector decides whether an interactive picker is appropriate before
    calling this function; noninteractive execution must not call it.
    """
    try:
        selection = _tk_selection()
    except FileSelectionUnavailable:
        selection = _zenity_selection()
    return _existing_path(selection)
