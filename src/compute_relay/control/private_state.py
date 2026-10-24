"""Small private-file boundary for the local controller's signing secret."""

import os
from pathlib import Path
import secrets
import stat

from django.core.exceptions import ImproperlyConfigured


def private_directory(path):
    path = Path(path).resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ImproperlyConfigured("The broker data directory must belong to this user and be private (0700)")
    return path


def signing_secret(path):
    """Create with restrictive permissions; reject links and untrusted files."""
    path = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    except OSError:
        raise ImproperlyConfigured("The broker signing secret could not be created safely") from None
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_urlsafe(64).encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ImproperlyConfigured("The broker signing secret must be a private regular file owned by this user")
            raw = stream.read(4097)
    except OSError:
        raise ImproperlyConfigured("The broker signing secret could not be read safely") from None
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeError:
        raise ImproperlyConfigured("The broker signing secret must contain valid text") from None
    if len(raw) > 4096 or len(value) < 32 or any(c in value for c in "\x00\r\n"):
        raise ImproperlyConfigured("The broker signing secret must contain between 32 and 4096 text bytes")
    return value
