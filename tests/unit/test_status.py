"""Hundreds of journal-bound attempts must need only bounded scheduler queries."""

import asyncio
import json
from uuid import uuid4

import pytest

from compute_relay.backends import FakeBackend, SlurmBackend
from compute_relay.backends.slurm import parse_observation
from compute_relay.remote.bundles import bundle_source
from compute_relay.remote.protocol import STATUS_BATCH_SIZE
from test_support.unit_test_remote_bundles import prepared, site


def namespace():
    result = {"__name__": "fixed_status_fixture"}
    exec(compile(bundle_source("jobs"), "<fixed-status>", "exec"), result)
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
    for offset in range(0, count, STATUS_BATCH_SIZE):
        chunk = items[offset:offset + STATUS_BATCH_SIZE]
        response = helper["execute_jobs"](site[0], {"operation": "observe", "protocol": "slurm-jobs/v1", "items": chunk})
        total_bytes += len(json.dumps(response))
        for item in chunk:
            native = item["native_identity"]
            value = response["items"][native["attempt_id"]]
            observation = parse_observation(native, value["queue"], value["accounting"])
            assert observation["state"] == ("running" if int(native["job_id"]) % 2 else "completed")
            assert observation["accounting"]["final"] == (int(native["job_id"]) % 2 == 0)
    assert len(calls) == 2 * ((count + STATUS_BATCH_SIZE - 1) // STATUS_BATCH_SIZE)
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
    response = helper["execute_jobs"]({}, {"operation": "observe", "protocol": "slurm-jobs/v1", "items": [
        {"native_identity": {"attempt_id": valid, "job_id": "7"}, "manifest_sha256": "a" * 64},
        {"native_identity": {"attempt_id": invalid, "job_id": "8"}, "manifest_sha256": "b" * 64}]})
    assert response["items"][invalid] == {"error": "IDENTITY_UNVERIFIED"}
    assert response["items"][valid]["queue"]["stdout"].startswith("7|")
    assert len(queries) == 1 and queries[0][1][queries[0][1].index("-j") + 1] == "7"


@pytest.mark.parametrize("items", [None, {}, [], [None] * (STATUS_BATCH_SIZE + 1)])
def test_invalid_batch_size_fails_before_scheduler_access(items):
    helper = namespace()
    with pytest.raises(ValueError):
        helper["execute_jobs"]({}, {"operation": "observe", "protocol": "slurm-jobs/v1", "items": items})


def test_one_live_attempt_uses_the_same_status_path_without_accounting_query():
    helper = namespace()
    helper["own_record"] = lambda *args: {"manifest": {"maximum_allocation": {"gpu_devices": 0}}}
    calls = []
    def scheduler(config, command, args):
        calls.append(command)
        return {"returncode": 0, "stdout": "7|RUNNING|marker|time", "stderr": ""}
    helper["scheduler"] = scheduler
    response = helper["execute_jobs"]({}, {"operation": "observe", "protocol": "slurm-jobs/v1", "items": [
        {"native_identity": {"attempt_id": "attempt", "job_id": "7"}, "manifest_sha256": "a" * 64}]})
    assert calls == ["squeue"]
    assert response["items"]["attempt"]["accounting"]["stdout"] == ""


def test_failed_accounting_query_cannot_settle_from_partial_stdout():
    helper = namespace()
    helper["own_record"] = lambda *args: {"manifest": {"maximum_allocation": {"gpu_devices": 0}}}
    native = {"attempt_id": "attempt", "job_id": "7", "submitted_at": "2026-10-21T12:00:00+00:00"}
    def scheduler(config, command, args):
        return {"returncode": int(command == "sacct"), "stderr": "", "stdout": "" if command == "squeue" else (
            "7|COMPLETED|1|1|cpu=1|0:0|2026-10-21T12:00:00|2026-10-21T12:00:00|2026-10-21T12:00:01|broker-attempt:attempt||")}
    helper["scheduler"] = scheduler
    response = helper["execute_jobs"]({}, {"operation": "observe", "protocol": "slurm-jobs/v1", "items": [
        {"native_identity": native, "manifest_sha256": "a" * 64}]})
    value = response["items"]["attempt"]
    assert value["accounting"]["returncode"] == 1
    assert parse_observation(native, value["queue"], value["accounting"])["accounting"] == {"final": False}


@pytest.mark.parametrize("backend_type", [SlurmBackend, FakeBackend])
@pytest.mark.parametrize("items", [None, {}, [], [None] * (STATUS_BATCH_SIZE + 1),
    [{"native_identity": {"attempt_id": "same"}}] * 2])
def test_backend_rejects_invalid_batches_before_transport_or_journal_access(backend_type, items, tmp_path):
    backend = backend_type({"state_directory": str(tmp_path), "ssh": {
        "host": "unused", "user": "owner", "known_hosts": str(tmp_path / "known_hosts")}})
    with pytest.raises(ValueError):
        asyncio.run(backend.observe(items))


def test_malformed_status_for_one_attempt_does_not_hide_another_observation(site):
    package, _ = prepared(site)
    native = {"attempt_id": str(uuid4()), "job_id": "7", "submitted_at": "2026-10-21T12:00:00+00:00"}
    other = {**native, "attempt_id": str(uuid4()), "job_id": "8"}
    class Transport:
        async def call_helper(self, source, request):
            assert request["operation"] == "observe" and len(request["items"]) == 2
            return {"items": {native["attempt_id"]: {"queue": {
                "returncode": 0, "stdout": f"7|RUNNING|broker-attempt:{native['attempt_id']}|2026-10-21T12:00:00"},
                "accounting": {"returncode": 0, "stdout": ""}}, other["attempt_id"]: []}}
    backend = SlurmBackend(site[0], transport=Transport())
    observations = asyncio.run(backend.observe([
        {"native_identity": item, "manifest": package["manifest"]} for item in (native, other)]))
    assert observations[native["attempt_id"]]["state"] == "running"
    assert observations[other["attempt_id"]]["state"] == "unknown"
    assert observations[other["attempt_id"]]["accounting"] == {"final": False}
