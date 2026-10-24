"""Offline protocol compatibility checks without migrating or contacting a cluster."""

import json
import os
import sqlite3
import sys
from contextlib import closing

from .local_state import AlreadyRunning, LocalState, LocalStateError, default_directory, validate_installation


_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_SAMPLES = 20


def _manifest(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        return None
    try:
        result = json.loads(value)
    except (ValueError, UnicodeError):
        return None
    return result if isinstance(result, dict) else None


def inspect_database(database):
    """Inspect protocol declarations; execution still verifies the exact package."""
    from .backends.preparation import management_protocol_supported, submission_helper_is_current

    summary = {"schema_version": "1.0", "management_compatible": True,
        "outstanding_attempts": 0, "unsupported_attempt_count": 0, "unsupported_attempts": [],
        "prepared_jobs_requiring_revision": 0, "unverified_input_jobs": 0,
        "upload_reservations_requiring_inspection": 0,
        "scope": "Stored protocol metadata only; no migrations, submissions or cluster connections."}
    # Avoid fetching an unexpectedly large or corrupt stored manifest.
    bounded_manifest = "CASE WHEN length(j.manifest) <= ? THEN j.manifest ELSE NULL END"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        connection.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "broker_uploadreservation" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(broker_uploadreservation)")}
            predicate = "AND (sha256='' OR expires_at IS NULL)" if {"sha256", "expires_at"} <= columns else ""
            summary["upload_reservations_requiring_inspection"] = connection.execute(
                "SELECT count(*) FROM broker_uploadreservation WHERE state='reserved' " + predicate).fetchone()[0]
        if "broker_jobfile" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(broker_jobfile)")}
            predicate = "WHERE source_kind='unverified'" if "source_kind" in columns else ""
            summary["unverified_input_jobs"] = connection.execute(
                "SELECT count(DISTINCT job_id) FROM broker_jobfile " + predicate).fetchone()[0]
        for attempt_id, raw in connection.execute(
                "SELECT a.id," + bounded_manifest + " FROM broker_attempt a "
                "JOIN broker_job j ON j.id=a.job_id "
                "WHERE a.accounting_state NOT IN ('settled','released') ORDER BY a.id",
                (_MAX_MANIFEST_BYTES,)):
            summary["outstanding_attempts"] += 1
            manifest = _manifest(raw)
            if manifest is None or not management_protocol_supported(manifest):
                summary["management_compatible"] = False
                summary["unsupported_attempt_count"] += 1
                if len(summary["unsupported_attempts"]) < _MAX_SAMPLES:
                    summary["unsupported_attempts"].append({"attempt_id": attempt_id,
                        "reason": "UNSUPPORTED_MANAGEMENT_PROTOCOL"})
        for raw, in connection.execute("SELECT " + bounded_manifest +
                " FROM broker_job j WHERE j.state='prepared'", (_MAX_MANIFEST_BYTES,)):
            manifest = _manifest(raw)
            if manifest is None or not submission_helper_is_current(manifest):
                summary["prepared_jobs_requiring_revision"] += 1
    return summary


def run(args):
    from .connector_profiles import _private_file

    try:
        with LocalState(args.data_directory or default_directory(), create=False) as state:
            installation = state.read("installation.json")
            if installation is None or not validate_installation(installation)["initialized"]:
                raise LocalStateError("Choose an initialized Compute Relay installation.")
            try:
                with state.lock():
                    descriptor = _private_file(state.descriptor, "control.sqlite3", os.O_RDONLY)
                    os.close(descriptor)
                    summary = inspect_database(state.directory / "control.sqlite3")
            except AlreadyRunning:
                raise LocalStateError("Stop Compute Relay and its connectors before checking an upgrade.") from None
        print(json.dumps(summary, sort_keys=True))
        if not summary["management_compatible"]:
            raise SystemExit(1)
    except LocalStateError as error:
        print(f"Compute Relay: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, sqlite3.Error):
        print("Compute Relay: could not check this stopped installation. Check its path, private permissions and database; no migration was performed.", file=sys.stderr)
        raise SystemExit(1) from None
