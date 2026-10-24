"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import hashlib
import json
import os
import re
import stat
import uuid


MAX_REQUEST = 150 * 1024 * 1024
MAX_FILE_TOTAL = 104857600
NOFOLLOW = os.O_NOFOLLOW
DIRECTORY = os.O_DIRECTORY
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def uuid_value(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Noncanonical attempt identity")
    return value


def components(path):
    if not isinstance(path, str) or not path or len(path) > 4096 or any(c in path for c in "\x00\r\n"):
        raise ValueError("Invalid path")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("Invalid path component")
    return parts


def open_directory(path, *, create=False, parent=None):
    if parent is None:
        if not isinstance(path, str) or not path.startswith("/") or path == "/":
            raise ValueError("A dedicated absolute remote root is required")
        fd = os.open("/", os.O_RDONLY | DIRECTORY)
        parts = components(path[1:])
    else:
        fd = os.dup(parent)
        parts = components(path)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_regular(directory, path, flags=os.O_RDONLY, mode=0o600):
    parts = components(path)
    fd = os.dup(directory)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        result = os.open(parts[-1], flags | NOFOLLOW, mode, dir_fd=fd)
        st = os.fstat(result)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            os.close(result)
            raise ValueError("Only regular files without additional hard links are supported")
        return result
    finally:
        os.close(fd)


def read_file(directory, path, cap, tail=False):
    fd = open_regular(directory, path)
    try:
        before = os.fstat(fd)
        if not tail and before.st_size > cap:
            raise ValueError("File exceeds artifact byte limit")
        if tail:
            os.lseek(fd, max(0, before.st_size - cap), os.SEEK_SET)
        chunks = []
        left = cap
        while left:
            chunk = os.read(fd, min(65536, left))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        after = os.fstat(fd)
        if not tail and (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Artifact changed during retrieval")
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_json(directory, name, data):
    temp = ".journal-" + uuid.uuid4().hex
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600, dir_fd=directory)
    try:
        payload = canonical(data)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temp, dir_fd=directory)
        except FileNotFoundError:
            pass


def load_json(directory, name):
    try:
        return json.loads(read_file(directory, name, 2 * 1024 * 1024))
    except FileNotFoundError:
        return None
