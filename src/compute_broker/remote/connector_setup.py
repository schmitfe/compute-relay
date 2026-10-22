"""Standalone, read-only directory selection for a human's personal connector.

The complete source may be sent to the protected remote Python interpreter.
There are no broker imports, shell commands, file-content reads or writes.
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
    deadline = time.monotonic() + SCAN_SECONDS
    fd = _open_directory(path, listing=True)
    try:
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
    finally:
        os.close(fd)


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
