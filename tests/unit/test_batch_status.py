"""Hundreds of journal-bound attempts must need only bounded scheduler queries."""

from copy import deepcopy
import json
from uuid import uuid4

import pytest

from compute_relay.backends.slurm import parse_observation
from compute_relay.remote.bundles import bundle_source
from test_support.unit_test_remote_bundles import prepared, site


def namespace():
    result = {"__name__": "fixed_batch_fixture"}
    exec(compile(bundle_source("batch_status"), "<fixed-batch>", "exec"), result)
    return result


@pytest.mark.parametrize("count", [200, 500])
def test_hundreds_of_attempts_use_two_queries_per_batch_without_quadratic_responses(site, count):
    helper = namespace()
    package, files = prepared(site)
    manifest = package["manifest"]
    import base64
    submitted, calls = {}, []
    def scheduler(config, command, arguments, **kwargs):
        calls.append(command)
        if command == "sbatch":
            return {"returncode": 0, "stdout": str(len(submitted) + 1), "stderr": ""}
        requested = arguments[arguments.index("-j") + 1].split(",")
        rows = []
        for job_id in requested:
            native = submitted[job_id]
            marker, timestamp = "broker-attempt:" + native["attempt_id"], native["submitted_at"]
            if command == "squeue" and int(job_id) % 2:
                rows.append(f"{job_id}|RUNNING|{marker}|{timestamp}")
            elif command == "sacct" and not int(job_id) % 2:
                rows.append(f"{job_id}|COMPLETED|1|1|cpu=1|0:0|{timestamp}|{timestamp}|{timestamp}|{marker}|{native['work_dir']}|")
        return {"returncode": 0, "stdout": "\n".join(rows), "stderr": ""}
    helper["scheduler"] = scheduler
    items = []
    for _ in range(count):
        response = helper["submit"](site[0], {"attempt_id": str(uuid4()), "manifest": manifest,
            "files": {path: base64.b64encode(value).decode() for path, value in files.items()}})
        native = response["native_identity"]
        submitted[native["job_id"]] = native
        items.append({"native_identity": native, "manifest_sha256": package["manifest_sha256"]})
    calls.clear()
    total_bytes = 0
    for offset in range(0, count, 64):
        chunk = items[offset:offset + 64]
        response = helper["observe_many"](site[0], {"operation": "observe_many", "protocol": "slurm-jobs/v1", "items": chunk})
        assert response["scheduler_queries"] == 2
        total_bytes += len(json.dumps(response))
        for item in chunk:
            native = item["native_identity"]
            value = response["items"][native["attempt_id"]]
            observation = parse_observation(native, value["queue"], value["accounting"])
            assert observation["state"] == ("running" if int(native["job_id"]) % 2 else "completed")
            assert observation["accounting"]["final"] == (int(native["job_id"]) % 2 == 0)
    assert len(calls) == 2 * ((count + 63) // 64)
    assert total_bytes < count * 1800


def test_invalid_journal_binding_isolated_without_querying_unowned_identity(site):
    helper = namespace()
    def own_record(config, identity, digest):
        if digest != "a" * 64:
            raise ValueError("wrong journal digest")
        return {"manifest": {"maximum_allocation": {"gpu_devices": 0}}}
    helper["own_record"] = own_record
    queries = []
    def scheduler(config, command, args):
        queries.append((command, args))
        return {"returncode": 0, "stdout": "7|RUNNING|marker|time", "stderr": ""}
    helper["scheduler"] = scheduler
    valid, invalid = str(uuid4()), str(uuid4())
    response = helper["observe_many"]({}, {"operation": "observe_many", "protocol": "slurm-jobs/v1", "items": [
        {"native_identity": {"attempt_id": valid, "job_id": "7"}, "manifest_sha256": "a" * 64},
        {"native_identity": {"attempt_id": invalid, "job_id": "8"}, "manifest_sha256": "b" * 64}]})
    assert response["items"][invalid] == {"error": "IDENTITY_UNVERIFIED"}
    assert response["items"][valid]["queue"]["stdout"].startswith("7|")
    assert len(queries) == 1 and queries[0][1][queries[0][1].index("-j") + 1] == "7"


@pytest.mark.parametrize("items", [[], [None] * 65])
def test_invalid_batch_size_fails_before_scheduler_access(items):
    helper = namespace()
    with pytest.raises(ValueError):
        helper["observe_many"]({}, {"operation": "observe_many", "protocol": "slurm-jobs/v1", "items": items})
