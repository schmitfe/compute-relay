"""Structured allocation accounting cannot acquire record framing from free text."""
from copy import deepcopy
import json

import pytest

from compute_relay.backends.slurm import parse_observation
from compute_relay.remote.scheduler import sacct_response, scheduler_rows
from compute_relay.remote.status import status_rows
from test_support.unit_test_remote_bundles import sacct_job

NATIVE = {"job_id": "42", "attempt_id": "55555555-5555-4555-8555-555555555555",
    "submitted_at": "2026-10-21T12:00:00+00:00", "work_dir": "/managed/original-attempt"}


def response(jobs, **extra):
    return {"returncode": 0, "stdout": json.dumps({"jobs": jobs, "errors": [], **extra})}


def observation(record):
    return parse_observation(NATIVE, {"returncode": 0, "stdout": ""}, sacct_response(response([record])))


@pytest.mark.parametrize("state,status,code,signal", [
    ("COMPLETED", "SUCCESS", 0, None), ("FAILED", "ERROR", 1, None),
    ("FAILED", "SIGNALED", None, 53), ("CANCELLED", "SIGNALED", None, 15),
    ("CANCELLED", "SUCCESS", 0, None), ("TIMEOUT", "ERROR", 1, None),
])
def test_terminal_json_preserves_state_usage_and_signal_exit_codes(state, status, code, signal):
    record = sacct_job(NATIVE, state=state, elapsed=3, cpus=2)
    record["exit_code"] = {"status": status, "return_code": code}
    if signal is not None:
        record["exit_code"]["signal"] = {"signal_id": signal}
    result = observation(record)
    assert result["terminal"] and result["native_state"] == state
    assert result["accounting"] == {"final": True, "cpu_core_seconds": 6, "gpu_device_seconds": 0}
    assert result["exit_code"] == f"{code or 0}:{signal or 0}"


def test_modern_wrappers_and_singleton_state_list_keep_typed_gpu_accounting():
    record = sacct_job(NATIVE, elapsed=10, cpus=8)
    record["state"]["current"] = ["COMPLETED"]
    def wrapped(value):
        return {"set": True, "infinite": False, "number": value}
    record["time"] = {key: wrapped(value) for key, value in record["time"].items()}
    record["tres"]["allocated"] += [{"type": "gres", "name": "gpu", "count": 2},
        {"type": "gres", "name": "gpu:a100", "count": 2}, {"type": "energy", "name": None, "count": None}]
    for entry in record["tres"]["allocated"][:3]:
        entry["count"] = wrapped(entry["count"])
    result = parse_observation(NATIVE, {"returncode": 0, "stdout": ""}, sacct_response(response([record])), expected_gpus=2)
    assert result["accounting"] == {"final": True, "cpu_core_seconds": 80, "gpu_device_seconds": 20}


@pytest.mark.parametrize("field", ["comment", "working_directory"])
@pytest.mark.parametrize("separator", ["|", "\n", "\r", "\x85", "\u2028", "\u2029"])
def test_free_text_cannot_add_even_a_syntactically_valid_accounting_record(field, separator):
    record = sacct_job(NATIVE)
    injected = separator + "42|COMPLETED|0|0|cpu=0|0:0|2026-10-21T12:00:00|2026-10-21T12:00:00|2026-10-21T12:00:01|marker|/work"
    if field == "comment": record[field]["job"] += injected
    else: record[field] += injected
    normalized = sacct_response(response([record]))
    assert normalized["returncode"] == 1 and normalized["stdout"] == ""
    assert observation(record)["accounting"] == {"final": False}


@pytest.mark.parametrize("path,value", [
    (("job_id",), True), (("time", "elapsed"), -1), (("time", "elapsed"), 1.5),
    (("time", "elapsed"), 2**53), (("time", "submission"), 0),
    (("time", "elapsed"), {"set": False, "infinite": False, "number": 1}),
    (("time", "elapsed"), {"set": True, "infinite": True, "number": 1}),
    (("state", "current"), []), (("state", "current"), ["COMPLETED", "RUNNING"]),
    (("tres", "allocated"), None), (("tres", "allocated"), []),
    (("tres", "allocated", 0, "type"), None), (("tres", "allocated", 0, "name"), "other"),
    (("tres", "allocated", 0, "count"), "1"), (("tres", "allocated", 0, "count"), None),
    (("exit_code",), None), (("exit_code", "return_code"), None),
    (("comment",), {}), (("working_directory",), None),
])
def test_missing_ambiguous_or_unbounded_json_fields_remain_unsettled(path, value):
    record = sacct_job(NATIVE)
    parent = record
    for key in path[:-1]: parent = parent[key]
    parent[path[-1]] = value
    assert sacct_response(response([record]))["returncode"] == 1
    assert observation(record)["accounting"] == {"final": False}


def test_duplicates_are_preserved_for_ambiguity_and_unrequested_identities_are_rejected():
    record = sacct_job(NATIVE)
    normalized = sacct_response(response([record, deepcopy(record)]))
    assert len(scheduler_rows(normalized, 11)) == 2
    result = parse_observation(NATIVE, {"returncode": 0, "stdout": ""}, normalized)
    assert result["accounting"] == {"final": False}
    unexpected = sacct_job({**NATIVE, "job_id": "43"})
    grouped = status_rows(sacct_response(response([record, unexpected])), ["42"], 11)
    assert grouped["42"]["returncode"] == 1


def test_full_marker_submission_window_and_workdir_fallback_still_bind_identity():
    record = sacct_job(NATIVE)
    record["comment"]["job"] = None
    assert observation(record)["accounting"]["final"]
    record["comment"]["job"] = "broker-attempt:wrong"
    assert not observation(record)["accounting"]["final"]
    record["comment"]["job"] = "broker-attempt:" + NATIVE["attempt_id"]
    record["time"]["submission"] -= 3600
    assert not observation(record)["accounting"]["final"]


def test_json_query_errors_and_bounds_have_no_delimited_fallback():
    valid = response([sacct_job(NATIVE)])
    bad = ["42|COMPLETED|1|1|cpu=1|0:0|submit|start|end|marker|workdir",
        '{"errors": [], "jobs": [], "jobs": []}', '{"errors": [], "jobs": [], "extra": NaN}',
        json.dumps({"jobs": []}), json.dumps({"errors": []}), "x" * (2 * 1024 * 1024 + 1)]
    for stdout in bad:
        assert sacct_response({"returncode": 0, "stdout": stdout})["returncode"] == 1
    assert sacct_response({**valid, "returncode": 1})["returncode"] == 1
    assert sacct_response(response([], errors=[{"error": "plugin unavailable"}]))["returncode"] == 1
    assert sacct_response(response([sacct_job(NATIVE)] * 1025))["returncode"] == 1
    assert sacct_response(response([sacct_job(NATIVE)], warnings=[{"description": "deprecated plugin"}]))["returncode"] == 0
