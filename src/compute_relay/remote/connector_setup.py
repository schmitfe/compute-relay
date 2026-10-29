"""Standalone directory setup for a human's personal connector.

The complete source may be sent to the protected remote Python interpreter.
There are no broker imports, shell commands, or file-content reads or writes.
Only explicitly requested mkdir operations create directories, mode 0700.
Symlink resolution is explicit in returned canonical paths; directory handles
subsequently refuse symlink substitutions while opening that canonical path.
"""

from __future__ import annotations

import json
import os
import posixpath
import pwd
import re
import stat
import sys
import time


MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
MAX_SCAN_ENTRIES = 2000
MAX_DIRECTORIES = 200
MAX_PATH_BYTES = 4096
MAX_COMPONENTS = 128
SCAN_SECONDS = 2


class SetupError(Exception):
    def __init__(self, code):
        self.code = code


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


def _path(value, *, root_allowed=True, job_root=False):
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise SetupError("INVALID_ROOT" if job_root else "INVALID_PATH")
    try:
        size = len(value.encode("utf-8", "strict"))
    except UnicodeError:
        raise SetupError("INVALID_ROOT" if job_root else "INVALID_PATH") from None
    limit = 240 if job_root else MAX_PATH_BYTES
    if (size > limit or value != posixpath.normpath(value) or value.startswith("//")
            or (value != "/" and any(part in {"", ".", ".."} for part in value.split("/")[1:]))):
        raise SetupError("INVALID_ROOT" if job_root else "INVALID_PATH")
    if not root_allowed and value == "/":
        raise SetupError("INVALID_ROOT")
    if job_root and not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        raise SetupError("INVALID_ROOT")
    if len(value.split("/")) - 1 > MAX_COMPONENTS:
        raise SetupError("PATH_TOO_DEEP")
    return value


def _canonical_existing(value):
    _path(value)
    return _path(os.path.realpath(value, strict=True))


def _home_path():
    # Account metadata avoids trusting a caller's arbitrary HOME environment.
    value = pwd.getpwuid(os.getuid()).pw_dir
    if (not isinstance(value, str) or not value.startswith("/") or "\x00" in value
            or len(value.encode("utf-8", "strict")) > MAX_PATH_BYTES):
        raise SetupError("PATH_UNAVAILABLE")
    return _path(os.path.realpath(value, strict=True))


def _open_directory(path, *, listing=False):
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.split("/")[1:]:
            if not part:
                continue
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        if listing:
            child = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _writable(fd):
    # Permissions are preflight metadata only: no test file is created.
    return os.access(".", os.W_OK | os.X_OK, dir_fd=fd, effective_ids=True)


def identity():
    return {"ok": True, "home": _home_path(), "cwd": _canonical_existing(os.getcwd())}


def browse(path=""):
    home = _home_path()
    path = _canonical_existing(home if path == "" else _path(path))
    fd = _open_directory(path, listing=True)
    try:
        return _browse_open_directory(path, home, fd)
    finally:
        os.close(fd)


def _browse_open_directory(path, home, fd):
    deadline = time.monotonic() + SCAN_SECONDS
    result = {"ok": True, "path": path, "parent": posixpath.dirname(path), "home": home,
              "entries": [], "writable": _writable(fd), "truncated": False, "scanned_entries": 0}
    used = len(_encoded(result))
    with os.scandir(fd) as entries:
        while True:
            if result["scanned_entries"] >= MAX_SCAN_ENTRIES or time.monotonic() >= deadline:
                result["truncated"] = True
                break
            try:
                entry = next(entries)
            except StopIteration:
                break
            result["scanned_entries"] += 1
            try:
                # No recursion or following child symlinks. Re-stat without
                # following because DirEntry metadata may already be stale.
                if not entry.is_dir(follow_symlinks=False):
                    continue
                entry.name.encode("utf-8", "strict")
                if not stat.S_ISDIR(os.stat(entry.name, dir_fd=fd, follow_symlinks=False).st_mode):
                    continue
                child = {"name": entry.name, "path": _path(posixpath.join(path, entry.name))}
                if used + len(_encoded(child)) + 1 > MAX_RESPONSE_BYTES - 128:
                    result["truncated"] = True
                    break
            except (OSError, UnicodeError, SetupError):
                result["truncated"] = True
                continue
            result["entries"].append(child)
            used += len(_encoded(child)) + 1
            if len(result["entries"]) >= MAX_DIRECTORIES:
                result["truncated"] = True
                break
    result["entries"].sort(key=lambda item: item["name"])
    return result


def _same_directory(first, second):
    return (stat.S_ISDIR(first.st_mode) and stat.S_ISDIR(second.st_mode)
            and (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino))


def _check_directory_path(fd, path):
    """Ensure an advertised absolute path still identifies the held directory."""
    try:
        current = _open_directory(path)
        try:
            if not _same_directory(os.fstat(fd), os.fstat(current)):
                raise SetupError("DIRECTORY_CHANGED")
        finally:
            os.close(current)
    except OSError:
        raise SetupError("DIRECTORY_CHANGED") from None


def mkdir(path, name, parents=False):
    if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name)
            or name in {".", ".."}):
        raise SetupError("INVALID_DIRECTORY_NAME")
    if type(parents) is not bool:
        raise SetupError("INVALID_REQUEST")
    home = _home_path()
    path = _path(path)
    try:
        parent = _canonical_existing(path)
    except FileNotFoundError:
        if not parents:
            raise
        selection = validate_root(path)
        _path(posixpath.join(selection["path"], name), root_allowed=False, job_root=True)
        parent = selection["existing_parent"]
        fd = _open_directory(parent)
        previous_umask = os.umask(0o077)
        try:
            for component in selection["path"][len(parent):].strip("/").split("/"):
                _check_directory_path(fd, parent)
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(component, getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                parent = posixpath.join(parent, component)
                _check_directory_path(fd, parent)
        finally:
            os.umask(previous_umask)
            os.close(fd)
    child_path = _path(posixpath.join(parent, name), root_allowed=False, job_root=True)
    parent_fd = _open_directory(parent)
    child_fd = None
    try:
        _check_directory_path(parent_fd, parent)
        created = False
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not _writable(parent_fd):
                raise SetupError("ROOT_NOT_WRITABLE")
            # This standalone helper is single-threaded. A temporary private
            # umask makes the new directory exactly 0700 even under umask 002.
            # No mode changes are ever applied to an existing object.
            previous_umask = os.umask(0o077)
            try:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                    created = True
                except FileExistsError:
                    pass  # Another request may have created the same directory.
            finally:
                os.umask(previous_umask)
            try:
                before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                raise SetupError("DIRECTORY_CHANGED") from None
        if not stat.S_ISDIR(before.st_mode):
            raise SetupError("DIRECTORY_CONFLICT")
        try:
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            held = os.fstat(child_fd)
            if not _same_directory(before, held):
                raise SetupError("DIRECTORY_CHANGED")
            result = _browse_open_directory(child_path, home, child_fd)
            after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_directory(held, after):
                raise SetupError("DIRECTORY_CHANGED")
            _check_directory_path(parent_fd, parent)
            _check_directory_path(child_fd, child_path)
        except OSError:
            raise SetupError("DIRECTORY_CHANGED") from None
        result["created"] = created
        return result
    finally:
        if child_fd is not None:
            os.close(child_fd)
        os.close(parent_fd)


def validate_root(path):
    _path(path, root_allowed=False, job_root=True)
    candidate, missing = path, []
    while True:
        try:
            existing = _canonical_existing(candidate)
            break
        except FileNotFoundError:
            # A dangling symlink is not an absent directory that jobs may safely
            # create later. Ordinary resolved home/ancestor aliases are allowed.
            try:
                if stat.S_ISLNK(os.lstat(candidate).st_mode):
                    raise SetupError("DANGLING_SYMLINK")
            except FileNotFoundError:
                pass
            if candidate == "/":
                raise SetupError("PATH_UNAVAILABLE") from None
            missing.append(posixpath.basename(candidate))
            candidate = posixpath.dirname(candidate)
    canonical = posixpath.join(existing, *reversed(missing)) if missing else existing
    _path(canonical, root_allowed=False, job_root=True)
    fd = _open_directory(existing)
    try:
        if not _writable(fd):
            raise SetupError("ROOT_NOT_WRITABLE")
        return {"ok": True, "path": canonical, "exists": not missing,
                "existing_parent": existing, "writable": True}
    finally:
        os.close(fd)


def handle_request(request):
    try:
        if not isinstance(request, dict):
            raise SetupError("INVALID_REQUEST")
        operation = request.get("operation")
        if operation == "identity" and set(request) == {"operation"}:
            return identity()
        if operation == "browse" and set(request) <= {"operation", "path"}:
            return browse(request.get("path", ""))
        if operation == "validate_root" and set(request) == {"operation", "path"}:
            return validate_root(request["path"])
        if operation == "mkdir" and set(request) in ({"operation", "path", "name"}, {"operation", "path", "name", "parents"}):
            return mkdir(request["path"], request["name"], parents=request.get("parents", False))
        raise SetupError("INVALID_REQUEST")
    except SetupError as error:
        return {"ok": False, "error_code": error.code}
    except NotADirectoryError:
        return {"ok": False, "error_code": "NOT_A_DIRECTORY"}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, NotImplementedError):
        return {"ok": False, "error_code": "PATH_UNAVAILABLE"}


def _pairs(items):
    value = {}
    for key, entry in items:
        if key in value:
            raise SetupError("INVALID_REQUEST")
        value[key] = entry
    return value


def _invalid_number(_value):
    raise SetupError("INVALID_REQUEST")


def main():
    try:
        payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(payload) > MAX_REQUEST_BYTES:
            raise SetupError("REQUEST_TOO_LARGE")
        request = json.loads(payload.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                             parse_constant=_invalid_number, parse_float=_invalid_number)
        result = handle_request(request)
        output = _encoded(result)
        if len(output) + 1 > MAX_RESPONSE_BYTES:
            raise SetupError("RESULT_TOO_LARGE")
    except SetupError as error:
        output = _encoded({"ok": False, "error_code": error.code})
    except Exception:
        output = _encoded({"ok": False, "error_code": "INVALID_REQUEST"})
    sys.stdout.buffer.write(output + b"\n")


if __name__ == "__main__":
    main()
