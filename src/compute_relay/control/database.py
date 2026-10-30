"""SQLite configuration for one local broker process."""

from contextlib import contextmanager
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction


def database_config(data_dir, environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("BROKER_DB_ENGINE", "sqlite") != "sqlite":
        raise ImproperlyConfigured("This single-user package supports SQLite only")
    name = environ.get("BROKER_DB_PATH", str(Path(data_dir) / "control.sqlite3"))
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ImproperlyConfigured("BROKER_DB_PATH must name a local SQLite file")
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": name,
        "OPTIONS": {"timeout": 15, "transaction_mode": "IMMEDIATE"}}


@contextmanager
def read_snapshot():
    """Keep multi-query GETs consistent without reserving SQLite's writer slot."""
    connection = transaction.get_connection()
    mode = connection.transaction_mode
    try:
        # This is the current thread's connection, never the shared DB settings.
        connection.transaction_mode = "DEFERRED"
        with transaction.atomic():
            connection.transaction_mode = mode
            yield
    finally:
        connection.transaction_mode = mode
