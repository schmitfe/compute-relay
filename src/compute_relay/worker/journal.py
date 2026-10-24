"""Crash-safe per-attempt intent records; never infer non-submission from a crash."""

from __future__ import annotations

import fcntl
import asyncio
import json
import logging
import os
import stat
import tempfile
from contextlib import contextmanager, asynccontextmanager
from pathlib import Path
from uuid import UUID

from .client import strict_json


class Journal:
    MAX_RECORD_BYTES = 16 * 1024 * 1024

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("journal directory must be private and owned by the worker")
        self.reports = self.root / "reports"
        self.reports.mkdir(mode=0o700, exist_ok=True)
        info = self.reports.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("report directory must be private and owned by the worker")
        self._report_cursor = ""

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
        return self._read(self._path(attempt_id))

    def _read(self, path):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077):
                raise ValueError("invalid journal record")
            data = source.read(self.MAX_RECORD_BYTES + 1)
        if len(data) > self.MAX_RECORD_BYTES:
            raise ValueError("oversized journal record")
        return strict_json(data)

    def write(self, attempt_id, record):
        self._write(self._path(attempt_id), record)

    def _write(self, target, record):
        content = json.dumps(record, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(content) > self.MAX_RECORD_BYTES:
            raise ValueError("oversized journal record")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, delete=False) as out:
                temporary = out.name
                out.write(content)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
            temporary = None
            fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temporary:
                os.unlink(temporary)

    def write_report(self, attempt_id, record):
        self._write(self.reports / self._path(attempt_id).name, record)

    def read_report(self, attempt_id):
        return self._read(self.reports / self._path(attempt_id).name)

    def clear_report(self, attempt_id, expected):
        if self.read_report(attempt_id) != expected:
            raise ValueError("pending report changed during delivery")
        (self.reports / self._path(attempt_id).name).unlink()
        fd = os.open(self.reports, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def pending_reports(self, *, limit=8):
        """Rotate over a bounded batch so one stale report cannot block others."""
        names = sorted(entry.name for entry in self.reports.iterdir() if entry.suffix == ".json")
        ordered = [name for name in names if name > self._report_cursor] + [name for name in names if name <= self._report_cursor]
        for name in ordered[:limit]:
            self._report_cursor = name
            try:
                attempt_id = str(UUID(Path(name).stem))
                record = self.read_report(attempt_id)
                if record is not None and record.get("phase") == "ready":
                    yield attempt_id, record
            except (OSError, ValueError, AttributeError):
                logging.getLogger(__name__).error("A durable worker report is invalid; retain it for local recovery")
