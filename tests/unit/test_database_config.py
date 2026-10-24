"""The personal package opens only its explicit local SQLite ledger."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from compute_relay.control.database import database_config


def test_sqlite_is_personal_default(tmp_path):
    config = database_config(tmp_path, {})
    assert config["ENGINE"] == "django.db.backends.sqlite3"
    assert config["NAME"] == str(tmp_path / "control.sqlite3")
    assert config["OPTIONS"]["transaction_mode"] == "IMMEDIATE"
    assert database_config(tmp_path, {"BROKER_DB_PATH": "/tmp/synthetic.sqlite3"})["NAME"] == "/tmp/synthetic.sqlite3"


@pytest.mark.parametrize("engine", ["postgresql", "mysql", "unknown", ""])
def test_other_engines_are_explicitly_rejected(tmp_path, engine):
    with pytest.raises(ImproperlyConfigured, match="SQLite only"):
        database_config(tmp_path, {"BROKER_DB_ENGINE": engine})


@pytest.mark.parametrize("name", ["", "invalid\x00path"])
def test_invalid_ledger_path_is_rejected(tmp_path, name):
    with pytest.raises(ImproperlyConfigured):
        database_config(tmp_path, {"BROKER_DB_PATH": name})
