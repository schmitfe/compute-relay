"""Explicit SQL configuration; passwords are read from private files, never URLs."""

import os
from pathlib import Path
import stat

from django.core.exceptions import ImproperlyConfigured


def integer(environ, name, default, minimum, maximum):
    try:
        value = int(environ.get(name, default))
    except (TypeError, ValueError):
        raise ImproperlyConfigured(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ImproperlyConfigured(f"{name} is outside its supported range")
    return value


def private_password(path):
    if not path:
        raise ImproperlyConfigured("PostgreSQL requires BROKER_DB_PASSWORD_FILE")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ImproperlyConfigured("Database password file must be regular and private (0600)")
            raw = stream.read(4097)
    except (OSError, ValueError):
        raise ImproperlyConfigured("Database password file cannot be read safely") from None
    try:
        value = raw.decode("utf-8").rstrip("\r\n")
    except UnicodeError:
        raise ImproperlyConfigured("Database password file must contain UTF-8 text") from None
    if not value or len(raw) > 4096 or any(char in value for char in "\x00\r\n"):
        raise ImproperlyConfigured("Database password file has invalid contents")
    return value


def database_config(data_dir, environ=None):
    environ = os.environ if environ is None else environ
    engine = environ.get("BROKER_DB_ENGINE", "sqlite")
    if engine == "sqlite":
        return {"ENGINE": "django.db.backends.sqlite3",
            "NAME": environ.get("BROKER_DB_PATH", str(Path(data_dir) / "control.sqlite3")),
            "OPTIONS": {"timeout": 15, "transaction_mode": "IMMEDIATE"}}
    if engine != "postgresql":
        raise ImproperlyConfigured("BROKER_DB_ENGINE must be sqlite or postgresql")
    if environ.get("BROKER_DB_PASSWORD") or environ.get("BROKER_POSTGRES_DSN"):
        raise ImproperlyConfigured("Use BROKER_DB_PASSWORD_FILE instead of a password environment variable or DSN")
    required = {name: environ.get(name, "") for name in ("BROKER_DB_NAME", "BROKER_DB_USER", "BROKER_DB_HOST")}
    if any(not value or any(c in value for c in "\x00\r\n") for value in required.values()):
        raise ImproperlyConfigured("PostgreSQL requires explicit database name, user and host")
    mode = environ.get("BROKER_DB_SSLMODE", "verify-full")
    if mode not in {"verify-full", "verify-ca", "require", "disable"}:
        raise ImproperlyConfigured("Use an explicit supported PostgreSQL TLS mode")
    options = {"sslmode": mode, "connect_timeout": integer(environ, "BROKER_DB_CONNECT_TIMEOUT", 5, 1, 30)}
    if environ.get("BROKER_DB_SSLROOTCERT"):
        options["sslrootcert"] = environ["BROKER_DB_SSLROOTCERT"]
    result = {"ENGINE": "django.db.backends.postgresql", "NAME": required["BROKER_DB_NAME"],
        "USER": required["BROKER_DB_USER"], "HOST": required["BROKER_DB_HOST"],
        "PORT": integer(environ, "BROKER_DB_PORT", 5432, 1, 65535),
        "PASSWORD": private_password(environ.get("BROKER_DB_PASSWORD_FILE")),
        "CONN_MAX_AGE": 0, "OPTIONS": options}
    if environ.get("BROKER_TEST_DB_NAME"):
        result["TEST"] = {"NAME": environ["BROKER_TEST_DB_NAME"]}
    return result
