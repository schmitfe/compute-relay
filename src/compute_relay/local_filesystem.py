"""Filesystem operations shared by local setup and workspace clients."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat


MAX_FILE_BYTES = 1024 * 1024


def absolute_path(value):
    path = Path(value).expanduser()
    if ".." in path.parts or any(ord(char) < 32 for char in str(path)):
        raise ValueError("Choose a path without parent traversal or control characters.")
    return path.absolute()


def check_path(path, *, directory=False, private=False):
    """Check every existing ancestor, including dangling symbolic links."""
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("Symbolic links are not allowed in setup paths.")
        is_directory = current != path or directory
        if (is_directory and not stat.S_ISDIR(info.st_mode)) or (not is_directory and not stat.S_ISREG(info.st_mode)):
            raise ValueError("A setup path has an unexpected file type.")
        if current == path and private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("Existing private broker files and directories must belong to you and exclude group/other access.")


@contextmanager
def parent_descriptor(path):
    """Traverse with no-follow directory descriptors, also during the write itself."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parent.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def read_regular(path, *, private=False):
    check_path(path, private=private)
    try:
        with parent_descriptor(path) as parent:
            descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Setup files must be ordinary files owned by you, without hard links.")
        if private and stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("The imported API-key file must exclude group/other access (use chmod 600).")
        value = stream.read(MAX_FILE_BYTES + 1)
        if len(value) > MAX_FILE_BYTES:
            raise ValueError("A setup file exceeds the size limit.")
        return value


def ensure_directory(path, created):
    if path.is_dir():
        check_path(path, directory=True)
        return
    ensure_directory(path.parent, created)
    with parent_descriptor(path) as parent:
        os.mkdir(path.name, mode=0o700, dir_fd=parent)
    created.append(path)
