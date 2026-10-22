import os

import pytest
from django.core.exceptions import ImproperlyConfigured

from compute_broker.control.database import database_config


def postgres(tmp_path, **changes):
    secret = tmp_path / "password"
    secret.write_text("synthetic-only-password\n")
    secret.chmod(0o600)
    return {"BROKER_DB_ENGINE": "postgresql", "BROKER_DB_NAME": "broker_test",
        "BROKER_DB_USER": "fixture", "BROKER_DB_HOST": "127.0.0.1",
        "BROKER_DB_PASSWORD_FILE": str(secret), **changes}


def test_sqlite_remains_explicit_personal_default(tmp_path):
    config = database_config(tmp_path, {})
    assert config["ENGINE"] == "django.db.backends.sqlite3"
    assert config["OPTIONS"]["transaction_mode"] == "IMMEDIATE"
    assert database_config(tmp_path, {"BROKER_DB_PATH": "/tmp/synthetic.sqlite3"})["NAME"] == "/tmp/synthetic.sqlite3"


def test_completed_migration_fences_accidental_sqlite_restart(tmp_path):
    (tmp_path / "control.sqlite3.migrated").write_text("synthetic cutover marker")
    with pytest.raises(ImproperlyConfigured, match="migrated"):
        database_config(tmp_path, {})


def test_postgres_uses_private_secret_and_separate_options(tmp_path):
    config = database_config(tmp_path, postgres(tmp_path))
    assert config["PASSWORD"] == "synthetic-only-password"
    assert config["ENGINE"] == "django.db.backends.postgresql"
    assert config["OPTIONS"] == {"sslmode": "verify-full", "connect_timeout": 5}
    assert config["CONN_MAX_AGE"] == 0 and "transaction_mode" not in config["OPTIONS"]


def test_postgres_explicit_test_and_tls_configuration(tmp_path):
    config = database_config(tmp_path, postgres(tmp_path, BROKER_DB_SSLMODE="disable",
        BROKER_DB_PORT="15432", BROKER_TEST_DB_NAME="test_broker", BROKER_DB_SSLROOTCERT="/ca/example.pem"))
    assert config["PORT"] == 15432 and config["TEST"] == {"NAME": "test_broker"}
    assert config["OPTIONS"]["sslrootcert"] == "/ca/example.pem"


@pytest.mark.parametrize("changes", [
    {"BROKER_DB_ENGINE": "unknown"}, {"BROKER_DB_NAME": ""}, {"BROKER_DB_HOST": ""},
    {"BROKER_DB_USER": ""}, {"BROKER_DB_HOST": "host\nother"}, {"BROKER_DB_PORT": "0"},
    {"BROKER_DB_PORT": "65536"}, {"BROKER_DB_PORT": "bad"}, {"BROKER_DB_CONNECT_TIMEOUT": "600"},
    {"BROKER_DB_SSLMODE": "prefer"}, {"BROKER_DB_PASSWORD_FILE": ""},
    {"BROKER_DB_PASSWORD": "synthetic-forbidden"}, {"BROKER_POSTGRES_DSN": "postgres://synthetic"},
])
def test_postgres_rejects_ambiguous_or_unsafe_configuration(tmp_path, changes):
    with pytest.raises(ImproperlyConfigured) as failure:
        database_config(tmp_path, postgres(tmp_path, **changes))
    assert "synthetic-forbidden" not in str(failure.value)


@pytest.mark.parametrize("value", [b"", b"\n", b"first\nsecond", b"a\x00b", b"\xff", b"x" * 4097])
def test_invalid_password_file_never_echoes_contents(tmp_path, value):
    environ = postgres(tmp_path)
    (tmp_path / "password").write_bytes(value)
    with pytest.raises(ImproperlyConfigured):
        database_config(tmp_path, environ)


def test_secret_must_be_private_regular_file_without_symlink(tmp_path):
    environ = postgres(tmp_path)
    (tmp_path / "password").chmod(0o644)
    with pytest.raises(ImproperlyConfigured):
        database_config(tmp_path, environ)
    (tmp_path / "password").chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "password")
    environ["BROKER_DB_PASSWORD_FILE"] = str(link)
    with pytest.raises(ImproperlyConfigured):
        database_config(tmp_path, environ)
    link.unlink()
    os.mkfifo(link, 0o600)
    with pytest.raises(ImproperlyConfigured):
        database_config(tmp_path, environ)
