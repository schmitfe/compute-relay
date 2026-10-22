"""Crash-safe per-attempt intent records; never infer non-submission from a crash."""

from __future__ import annotations

import fcntl
import asyncio
import json
import os
import stat
import tempfile
from contextlib import contextmanager, asynccontextmanager
from pathlib import Path
from uuid import UUID

from .client import strict_json


class Journal:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("journal directory must be private and owned by the worker")

    def _path(self, attempt_id):
        return self.root / (str(UUID(str(attempt_id))) + ".json")

    @contextmanager
    def locked(self, attempt_id):
        path = self._path(attempt_id).with_suffix(".lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @asynccontextmanager
    async def async_locked(self, attempt_id, *, timeout=5):
        """Honor the same process fence without blocking the event loop on flock."""
        path = self._path(attempt_id).with_suffix(".lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            async with asyncio.timeout(timeout):
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def read(self, attempt_id):
        try:
            fd = os.open(self._path(attempt_id), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("invalid journal record")
            data = source.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("oversized journal record")
        return strict_json(data)

    def write(self, attempt_id, record):
        target = self._path(attempt_id)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.root, delete=False) as out:
                temporary = out.name
                out.write(json.dumps(record, allow_nan=False, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8"))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
            temporary = None
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temporary:
                os.unlink(temporary)
