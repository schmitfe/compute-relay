"""Read-only upgrade checks identify stranded protocols without touching jobs."""

from argparse import Namespace
from copy import deepcopy
import json
import sqlite3
from uuid import uuid4

import pytest

from compute_relay.backends import prepare
from compute_relay.local_state import LocalState
from compute_relay.upgrade_check import inspect_database, run
from test_support.unit_test_backends import sample


def installation(tmp_path, manifests):
    directory = tmp_path / "state"
    with LocalState(directory) as state:
        state.write("installation.json", {"schema_version": "1.0", "installation_id": str(uuid4()),
            "owner_id": str(uuid4()), "portal_auth": "none", "port": 8768,
            "extensions": [], "initialized": True})
    database = directory / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE broker_job (id TEXT PRIMARY KEY,state TEXT,manifest TEXT)")
        connection.execute("CREATE TABLE broker_attempt (id TEXT PRIMARY KEY,job_id TEXT,accounting_state TEXT)")
        for manifest, accounting in manifests:
            job_id = str(uuid4())
            connection.execute("INSERT INTO broker_job VALUES (?, 'prepared', ?)",
                (job_id, json.dumps(manifest)))
            if accounting is not None:
                connection.execute("INSERT INTO broker_attempt VALUES (?, ?, ?)",
                    (str(uuid4()), job_id, accounting))
    database.chmod(0o600)
    return directory, database


def manifest(sample):
    return prepare(*sample[:3], str(uuid4()))["manifest"]


def test_current_protocols_are_compatible_and_database_is_unchanged(tmp_path, sample, capsys):
    current = manifest(sample)
    directory, database = installation(tmp_path, [(current, "reserved"), (current, "settled")])
    saved = database.read_bytes()
    run(Namespace(data_directory=str(directory)))
    result = json.loads(capsys.readouterr().out)
    assert result["management_compatible"] is True
    assert result["outstanding_attempts"] == 1
    assert result["prepared_jobs_requiring_revision"] == 0
    assert database.read_bytes() == saved


def test_helper_change_requires_new_submission_revision_but_preserves_management(tmp_path, sample):
    old = manifest(sample)
    old["helper_sha256"] = "0" * 64
    _, database = installation(tmp_path, [(old, "settlement_pending")])
    result = inspect_database(database)
    assert result["management_compatible"] is True
    assert result["outstanding_attempts"] == 1
    assert result["prepared_jobs_requiring_revision"] == 1


def test_unknown_live_protocol_blocks_upgrade_with_bounded_fixed_diagnostics(tmp_path, sample, capsys):
    invalid = deepcopy(manifest(sample))
    invalid["adapter_version"] = "private-unrecognized-value"
    directory, _ = installation(tmp_path, [(invalid, "reserved")] * 25)
    with pytest.raises(SystemExit) as error:
        run(Namespace(data_directory=str(directory)))
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "private-unrecognized-value" not in output.out + output.err
    result = json.loads(output.out)
    assert result["unsupported_attempt_count"] == 25
    assert len(result["unsupported_attempts"]) == 20
    assert result["management_compatible"] is False


def test_completed_unsupported_protocol_does_not_strand_an_allocation(tmp_path, sample):
    old = manifest(sample)
    old["adapter_version"] = "unknown/v0"
    _, database = installation(tmp_path, [(old, "settled"), (old, "released")])
    assert inspect_database(database)["management_compatible"] is True


def test_missing_or_unsafe_state_never_creates_a_database(tmp_path, capsys):
    missing = tmp_path / "not-an-installation"
    with pytest.raises(SystemExit):
        run(Namespace(data_directory=str(missing)))
    assert not missing.exists()
    assert "Traceback" not in capsys.readouterr().err


def test_running_application_must_stop_before_the_upgrade_check(tmp_path, sample, capsys):
    directory, _ = installation(tmp_path, [(manifest(sample), "reserved")])
    with LocalState(directory) as state, state.lock():
        with pytest.raises(SystemExit):
            run(Namespace(data_directory=str(directory)))
    assert "Stop Compute Relay" in capsys.readouterr().err


def test_pre_fix_uploads_and_inputs_are_flagged_without_inventing_authority(tmp_path, sample):
    _, database = installation(tmp_path, [(manifest(sample), "reserved")])
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE broker_uploadreservation (state TEXT)")
        connection.execute("INSERT INTO broker_uploadreservation VALUES ('reserved')")
        connection.execute("CREATE TABLE broker_jobfile (job_id TEXT)")
        connection.execute("INSERT INTO broker_jobfile VALUES ('old-job')")
    result = inspect_database(database)
    assert result["upload_reservations_requiring_inspection"] == 1
    assert result["unverified_input_jobs"] == 1
    assert result["management_compatible"] is True
