"""Backend contracts without cluster credentials or uploaded-code execution."""

import asyncio
from test_support.unit_test_backends import ATTEMPT, REVISION, metadata_module_catalog, observation, sample

import base64
import copy
import json
from pathlib import Path

import pytest
import rfc8785

from compute_relay.backends import (
    BackendError, FakeBackend, SlurmBackend, canonical_json_bytes, prepare, verify_package,
)
from compute_relay.backends.preparation import sha256, JOB_PROTOCOL
from compute_relay.backends.slurm import parse_observation
from compute_relay.backends.discovery import parse_discovery
from compute_relay.backends.environments import EnvironmentProfileError, render_environment, validate_environment_config
from compute_relay.remote import helper
from compute_relay.transports import OpenSSHTransport, TransportError






def package(sample):
    request, environment, connection, content = sample
    prepared = prepare(request, environment, connection, REVISION)
    files = {"main.py": content, **{path: source.encode() for path, source in prepared["generated_files"].items()}}
    return prepared, files


def test_prepare_is_deterministic_and_reserves_requested_walltime(sample):
    prepared, files = package(sample)
    assert prepared == prepare(*sample[:3], REVISION)
    manifest = prepared["manifest"]
    assert manifest["maximum_allocation"]["time_limit_seconds"] == 60
    assert manifest["reservation"] == {"cpu_core_seconds": 120, "gpu_device_seconds": 0}
    assert "#SBATCH --time=1\n" in prepared["generated_files"]["_broker/batch.sh"]
    assert "#SBATCH --no-requeue\n" in prepared["generated_files"]["_broker/batch.sh"]
    assert "exec /usr/bin/python3 ./main.py\n" in prepared["generated_files"]["_broker/batch.sh"]
    verify_package(manifest, prepared["manifest_sha256"], files)


def test_canonical_manifest_matches_jcs_for_the_supported_domain(sample):
    prepared, _ = package(sample)
    prepared["manifest"]["request"]["command"]["arguments"] = ["Ünicode", "quote\"\\\n"]
    assert canonical_json_bytes(prepared["manifest"]) == rfc8785.dumps(prepared["manifest"])
    for invalid in (float("nan"), 1.2, 2**53, "\ud800", {"é": "unicode key"}):
        with pytest.raises(BackendError):
            canonical_json_bytes(invalid)


@pytest.mark.parametrize("path", ["../main.py", "/main.py", "a//b", "a/./b", "a/../b", "_broker/evil.sh", "main.py\n", "stdout.log", "stderr.log"])
def test_invalid_or_reserved_input_paths_are_rejected(sample, path):
    sample[0]["files"][0]["path"] = path
    with pytest.raises(BackendError):
        package(sample)


def test_package_rejects_changed_content_and_extra_files(sample):
    prepared, files = package(sample)
    files["main.py"] += b"# changed"
    with pytest.raises(BackendError):
        verify_package(prepared["manifest"], prepared["manifest_sha256"], files)
    prepared, files = package(sample)
    files["extra.py"] = b""
    with pytest.raises(BackendError):
        verify_package(prepared["manifest"], prepared["manifest_sha256"], files)


def test_effective_resource_rounding_is_checked_before_admission(sample):
    sample[2]["site_profile"]["allocated_cpus"] = 8
    with pytest.raises(BackendError, match="Effective CPU"):
        package(sample)


def test_requested_walltime_must_be_an_explicit_whole_minute(sample):
    sample[0]["resources"]["time_limit_seconds"] = 61
    with pytest.raises(BackendError, match="whole number of minutes"):
        package(sample)


def test_native_preparation_needs_no_margin_configuration(sample):
    sample[2]["backend"] = "slurm"
    sample[2]["site_profile"] = {"partitions": ["debug"], "accounts": ["broker"]}
    prepared, _ = package(sample)
    assert prepared["manifest"]["reservation"] == {"cpu_core_seconds": 120, "gpu_device_seconds": 0}


def test_job_arguments_are_quoted_as_data(sample):
    import shlex

    sample[0]["command"]["arguments"] = ["; touch /tmp/do-not-run", "$(id)", "a'b", "line\nbreak"]
    prepared, _ = package(sample)
    script = prepared["generated_files"]["_broker/batch.sh"]
    command = script[script.index("exec ") + 5:].strip()
    assert shlex.split(command) == ["/usr/bin/python3", "./main.py", *sample[0]["command"]["arguments"]]


def test_fake_persists_identity_without_executing_uploaded_source(sample, tmp_path):
    marker = tmp_path / "should-not-exist"
    content = f"from pathlib import Path; Path({str(marker)!r}).touch()".encode()
    sample[0]["files"][0].update(sha256=sha256(content), size_bytes=len(content))
    sample = (*sample[:3], content)
    prepared, files = package(sample)
    backend = FakeBackend(sample[2])
    result = asyncio.run(backend.submit(ATTEMPT, prepared["manifest"], files))
    assert not marker.exists()
    restarted = FakeBackend(sample[2])
    assert asyncio.run(restarted.reconcile(ATTEMPT, prepared["manifest"]))["native_identity"] == result["native_identity"]
    assert asyncio.run(restarted.observe([{"native_identity": result["native_identity"], "manifest": prepared["manifest"]}]))[ATTEMPT]["accounting"] == {"final": True, "cpu_core_seconds": 2, "gpu_device_seconds": 0}
    collected = asyncio.run(restarted.collect(result["native_identity"], ["results/metrics.json"], manifest=prepared["manifest"]))
    assert json.loads(collected["artifacts"]["results/metrics.json"])["executed_uploaded_code"] is False
    with pytest.raises(BackendError):
        asyncio.run(restarted.collect(result["native_identity"], ["not-declared"], manifest=prepared["manifest"]))


def test_fake_cancellation_is_durable(sample):
    prepared, files = package(sample)
    backend = FakeBackend(sample[2])
    identity = asyncio.run(backend.submit(ATTEMPT, prepared["manifest"], files))["native_identity"]
    assert asyncio.run(backend.cancel(identity, manifest=prepared["manifest"]))["outcome"] == "cancel_requested"
    assert asyncio.run(FakeBackend(sample[2]).observe([{"native_identity": identity, "manifest": prepared["manifest"]}]))[ATTEMPT]["state"] == "cancelled"


def test_native_transport_timeout_does_not_resubmit(sample):
    class Unavailable:
        calls = 0

        async def call_helper(self, source, request):
            self.calls += 1
            assert "ssh" not in request["config"]
            raise TransportError("Lost reply after Slurm may have accepted")

    prepared, files = package(sample)
    transport = Unavailable()
    backend = SlurmBackend(sample[2], transport=transport)
    assert asyncio.run(backend.submit(ATTEMPT, prepared["manifest"], files))["outcome"] == "submission_uncertain"
    assert transport.calls == 1
    assert asyncio.run(backend.reconcile(ATTEMPT, prepared["manifest"]))["outcome"] == "submission_uncertain"
    assert transport.calls == 2




def test_accounting_uses_allocated_time_without_double_counting_typed_gpu():
    row = f"42|COMPLETED|10|8|cpu=8,gres/gpu=2,gres/gpu:a100=2|0:0|2026-10-21T12:00:00|2026-10-21T12:00:01|2026-10-21T12:00:11|broker-attempt:{ATTEMPT}|/managed/original-attempt|\n"
    result = observation(row, gpus=2)
    assert result["accounting"] == {"final": True, "cpu_core_seconds": 80, "gpu_device_seconds": 20}
    assert result["terminal"] is True


@pytest.mark.parametrize("row", ["", f"42|COMPLETED|10|8|cpu=8|0:0|2026-10-21T12:00:00|start|end|broker-attempt:{ATTEMPT}|/managed/original-attempt|\n"])
def test_missing_gpu_or_accounting_data_never_becomes_zero_use(row):
    assert observation(row, gpus=1)["accounting"]["final"] is False


def test_requeues_duplicate_records_and_old_job_ids_do_not_settle():
    row = f"42|COMPLETED|10|8|cpu=8|0:0|2026-10-21T12:00:00|start|end|broker-attempt:{ATTEMPT}|/managed/original-attempt|\n"
    assert observation(row + row)["accounting"]["final"] is False
    old = row.replace("2026-10-21T12:00:00", "2026-10-20T12:00:00")
    assert observation(old)["accounting"]["final"] is False


@pytest.fixture
def remote_request(sample, tmp_path):
    prepared, files = package(sample)
    return {"operation": "submit", "protocol": JOB_PROTOCOL, "manifest_sha256": prepared["manifest_sha256"], "config": {"remote_root": str(tmp_path / "remote")}, "attempt_id": ATTEMPT,
            "manifest": prepared["manifest"], "files": {path: base64.b64encode(data).decode() for path, data in files.items()}}


def test_remote_journal_replay_never_submits_the_same_attempt_twice(remote_request, monkeypatch):
    calls = []

    def scheduler(config, command, args, **kwargs):
        calls.append((command, args))
        return {"returncode": 0, "stdout": "42;fixture\n", "stderr": ""}

    monkeypatch.setattr(helper, "scheduler", scheduler)
    first = helper.execute(remote_request["config"], remote_request)
    assert first["outcome"] == "submitted"
    second = helper.execute(remote_request["config"], remote_request)
    assert second["native_identity"] == first["native_identity"]
    attempt_dir = Path(remote_request["config"]["remote_root"]) / first["attempt_path"]
    assert attempt_dir.joinpath("_broker/batch.sh").stat().st_mode & 0o777 == 0o500
    assert attempt_dir.joinpath("main.py").stat().st_mode & 0o777 == 0o400
    assert len(calls) == 1
    assert calls[0][0] == "sbatch"
    assert "--comment=broker-attempt:" + ATTEMPT in calls[0][1]
    assert not any("--cpus" in item for item in calls[0][1])


def test_remote_acceptance_response_loss_retains_intent_and_recovers_marker(remote_request, monkeypatch):
    import datetime as dt

    calls = []

    def scheduler(config, command, args, **kwargs):
        calls.append(command)
        if command == "sbatch":
            raise TimeoutError("accepted, but response lost")
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return {"returncode": 0, "stdout": "42|broker-attempt:" + ATTEMPT + "|" + timestamp + "\n", "stderr": ""}

    monkeypatch.setattr(helper, "scheduler", scheduler)
    result = helper.execute(remote_request["config"], remote_request)
    assert result["outcome"] == "submission_uncertain"
    assert helper.execute(remote_request["config"], remote_request)["outcome"] == "submission_uncertain"
    reconcile = {**remote_request, "operation": "reconcile"}
    assert helper.execute(remote_request["config"], reconcile)["native_identity"]["job_id"] == "42"
    assert calls == ["sbatch", "squeue"]


def test_nonzero_sbatch_rpc_result_never_refunds_or_retries(remote_request, monkeypatch):
    calls = []

    def scheduler(config, command, args, **kwargs):
        calls.append(command)
        if command == "sbatch":
            return {"returncode": 1, "stdout": "", "stderr": "Controller accepted but response connection failed"}
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(helper, "scheduler", scheduler)
    result = helper.execute(remote_request["config"], remote_request)
    assert result["outcome"] == "submission_uncertain"
    assert helper.execute(remote_request["config"], remote_request)["outcome"] == "submission_uncertain"
    reconcile = {**remote_request, "operation": "reconcile"}
    assert helper.execute(remote_request["config"], reconcile)["outcome"] == "submission_uncertain"
    assert calls == ["sbatch", "squeue", "sacct"]


def test_completed_job_lost_reply_can_recover_from_accounting_marker(remote_request, monkeypatch):
    import datetime as dt

    def scheduler(config, command, args, **kwargs):
        if command == "sbatch":
            raise TimeoutError("response lost")
        if command == "squeue":
            return {"returncode": 0, "stdout": "", "stderr": ""}
        assert "--format=JobIDRaw,Comment,Submit" in args
        assert any(arg.startswith("--starttime=") for arg in args)
        assert any(arg.startswith("--user=") for arg in args)
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return {"returncode": 0, "stdout": f"42|broker-attempt:{ATTEMPT}|{timestamp}|\n", "stderr": ""}

    monkeypatch.setattr(helper, "scheduler", scheduler)
    assert helper.execute(remote_request["config"], remote_request)["outcome"] == "submission_uncertain"
    reconcile = {**remote_request, "operation": "reconcile"}
    assert helper.execute(remote_request["config"], reconcile)["native_identity"]["job_id"] == "42"


def test_remote_output_retrieval_refuses_symlink_escape(remote_request, monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "scheduler", lambda *args, **kw: {"returncode": 0, "stdout": "42\n", "stderr": ""})
    result = helper.execute(remote_request["config"], remote_request)
    directory = Path(remote_request["config"]["remote_root"]) / result["attempt_path"]
    (directory / "results").mkdir()
    outside = tmp_path / "private"
    outside.write_text("must not be returned")
    (directory / "results" / "metrics.json").symlink_to(outside)
    with pytest.raises(OSError):
        helper.execute(remote_request["config"], {**remote_request, "operation": "collect", "native_identity": result["native_identity"],
                                                   "outputs": ["results/metrics.json"]})


def test_ssh_mfa_fails_closed_instead_of_reauthenticating():
    config = {"ssh": {"host": "cluster", "user": "broker", "known_hosts": "/protected/known_hosts",
                       "mode": "mfa", "control_path": "/protected/session.sock"}}
    argv = OpenSSHTransport(config).argv()
    assert "ControlPath=/protected/session.sock" in argv
    assert "ProxyCommand=/bin/false" in argv
    assert "ForwardAgent=no" in argv
    assert "PermitLocalCommand=no" in argv
    assert argv[1:3] == ["-F", "/dev/null"]
    assert "BatchMode=yes" in argv


def test_discovery_separates_visibility_from_access_without_timing_margins():
    response = {
        "version": {"returncode": 0, "stdout": "slurm 22.05\n"},
        "nodes": {"returncode": 0, "stdout": "gpu01|gpu*|32|256000|gpu:a100:4|idle|2|8|2\n"},
        "partitions": {"returncode": 0, "stdout": "PartitionName=gpu MaxTime=01:00:00 OverTimeLimit=2\n"},
        "settings": {"returncode": 0, "stdout": "KillWait = 5 sec\nOverTimeLimit = 0 min\nJobRequeue = 0\n"},
        "associations": {"returncode": 1, "stdout": "", "stderr": "not permitted"},
    }
    result = parse_discovery(response, {})
    assert result["nodes"][0]["gres"] == "gpu:a100:4"
    assert result["nodes"][0]["partition"] == "gpu"
    assert result["association_query_status"] == "unknown"
    assert "timing_candidates" not in result
    assert result["scheduler_settings"]["KillWait"] == "5 sec"
    assert not any("finite verified site margin" in note for note in result["notes"])


def test_helper_discovery_performs_only_fixed_readonly_queries(monkeypatch):
    calls = []

    def scheduler(config, command, args, **kwargs):
        calls.append((command, args))
        if args == ["show", "config"]:
            return {"returncode": 0, "stdout": "KillWait = 5 sec\nOverTimeLimit = 0 min\nAuthInfo = not-returned\n", "stderr": ""}
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(helper, "scheduler", scheduler)
    result = helper.execute({}, {"operation": "discover"})
    assert "AuthInfo" not in result["settings"]["stdout"]
    assert {command for command, _ in calls} == {"sinfo", "scontrol", "sacctmgr"}
    association_call = next(args for command, args in calls if command == "sacctmgr")
    assert "show" in association_call and "association" in association_call
    assert any(item.startswith("user=") for item in association_call)


def test_terminal_state_without_complete_timestamp_does_not_settle():
    row = f"42|COMPLETED|10|8|cpu=8|0:0|2026-10-21T12:00:00|2026-10-21T12:00:01|Unknown|broker-attempt:{ATTEMPT}|/managed/original-attempt|\n"
    assert observation(row)["terminal"] is True
    assert observation(row)["accounting"]["final"] is False


def test_nonempty_account_and_qos_allowlists_require_explicit_requests(sample):
    del sample[0]["slurm"]["account"]
    with pytest.raises(BackendError, match="explicit scheduler account"):
        package(sample)
    sample[0]["slurm"]["account"] = "broker"
    sample[2]["site_profile"]["qos"] = ["normal"]
    with pytest.raises(BackendError, match="explicit scheduler qos"):
        package(sample)
    sample[0]["slurm"]["qos"] = "normal"
    package(sample)


def test_future_reused_job_id_cannot_settle_the_original_attempt():
    row = f"42|COMPLETED|10|8|cpu=8|0:0|2026-10-22T12:00:00|2026-10-22T12:00:01|2026-10-22T12:00:11|broker-attempt:{ATTEMPT}|/managed/original-attempt|\n"
    result = observation(row)
    assert result["state"] == "unknown"
    assert result["accounting"]["final"] is False


def test_accounting_without_comment_requires_exact_managed_work_directory():
    row = "42|COMPLETED|10|8|cpu=8|0:0|2026-10-21T12:00:00|2026-10-21T12:00:01|2026-10-21T12:00:11||/managed/original-attempt|\n"
    assert observation(row)["accounting"]["final"] is True
    assert observation(row.replace("/managed/original-attempt", "/other/job"))["accounting"]["final"] is False
    conflict = row.replace("||/managed", "|different-marker|/managed")
    assert observation(conflict)["accounting"]["final"] is False


def test_live_observation_requires_the_original_marker_and_submit_window():
    identity = {"job_id": "42", "attempt_id": ATTEMPT, "submitted_at": "2026-10-21T12:00:00+00:00"}
    correct = f"42|RUNNING|broker-attempt:{ATTEMPT}|2026-10-21T12:00:01\n"
    unknown_accounting = {"returncode": 0, "stdout": ""}
    assert parse_observation(identity, {"returncode": 0, "stdout": correct}, unknown_accounting)["state"] == "running"
    for wrong in (correct.replace(ATTEMPT, "other-attempt"), correct.replace("2026-10-21", "2026-10-22")):
        assert parse_observation(identity, {"returncode": 0, "stdout": wrong}, unknown_accounting)["state"] == "unknown"


@pytest.mark.parametrize("wrong_identity", ["comment", "future_submit"])
def test_cancel_never_invokes_scancel_for_a_reused_or_mismatched_job(remote_request, monkeypatch, wrong_identity):
    calls = []

    def scheduler(config, command, args, **kwargs):
        import datetime as dt

        calls.append(command)
        if command == "sbatch":
            return {"returncode": 0, "stdout": "42\n", "stderr": ""}
        assert command == "squeue"
        marker = "other-attempt" if wrong_identity == "comment" else "broker-attempt:" + ATTEMPT
        submitted = dt.datetime.now(dt.timezone.utc) + (dt.timedelta(days=1) if wrong_identity == "future_submit" else dt.timedelta())
        return {"returncode": 0, "stdout": f"42|{marker}|{submitted.strftime('%Y-%m-%dT%H:%M:%S')}\n", "stderr": ""}

    monkeypatch.setattr(helper, "scheduler", scheduler)
    receipt = helper.execute(remote_request["config"], remote_request)
    result = helper.execute(remote_request["config"], {**remote_request, "operation": "cancel", "native_identity": receipt["native_identity"]})
    assert result["outcome"] == "identity_mismatch"
    assert calls == ["sbatch", "squeue"]


def test_cancel_validates_identity_before_requesting_cancellation(remote_request, monkeypatch):
    import datetime as dt

    calls = []

    def scheduler(config, command, args, **kwargs):
        calls.append((command, args))
        if command == "sbatch":
            return {"returncode": 0, "stdout": "42\n", "stderr": ""}
        if command == "squeue":
            submitted = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            return {"returncode": 0, "stdout": f"42|broker-attempt:{ATTEMPT}|{submitted}\n", "stderr": ""}
        assert command == "scancel"
        assert any(arg.startswith("--user=") for arg in args)
        assert "--name=test-job" in args
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(helper, "scheduler", scheduler)
    receipt = helper.execute(remote_request["config"], remote_request)
    result = helper.execute(remote_request["config"], {**remote_request, "operation": "cancel", "native_identity": receipt["native_identity"]})
    assert result["outcome"] == "cancel_requested"
    assert [command for command, _ in calls] == ["sbatch", "squeue", "scancel"]


@pytest.mark.parametrize("cpus,tres,expected_cpu", [(1, "cpu=1", 1), (0, "", 0)])
def test_zero_elapsed_only_costs_zero_when_no_resources_were_allocated(cpus, tres, expected_cpu):
    row = f"42|COMPLETED|0|{cpus}|{tres}|0:0|2026-10-21T12:00:00|2026-10-21T12:00:00|2026-10-21T12:00:00|broker-attempt:{ATTEMPT}|/managed/original-attempt|\n"
    result = observation(row)
    assert result["accounting"] == {"final": True, "cpu_core_seconds": expected_cpu, "gpu_device_seconds": 0}
    assert result["scheduler_elapsed_seconds"] == 0


def test_zero_elapsed_gpu_allocation_is_metered_for_one_second():
    row = f"42|COMPLETED|0|1|cpu=1,gres/gpu=2|0:0|2026-10-21T12:00:00|2026-10-21T12:00:00|2026-10-21T12:00:00|broker-attempt:{ATTEMPT}|/managed/original-attempt|\n"
    assert observation(row, gpus=2)["accounting"] == {"final": True, "cpu_core_seconds": 1, "gpu_device_seconds": 2}


def test_simulated_zero_second_job_still_spends_allowance(sample):
    sample[2]["fake_elapsed_seconds"] = 0
    prepared, files = package(sample)
    backend = FakeBackend(sample[2])
    native = asyncio.run(backend.submit(ATTEMPT, prepared["manifest"], files))["native_identity"]
    assert asyncio.run(backend.observe([{"native_identity": native, "manifest": prepared["manifest"]}]))[ATTEMPT]["accounting"] == {"final": True, "cpu_core_seconds": 2, "gpu_device_seconds": 0}


def test_preempted_no_requeue_job_has_final_accounting():
    row = f"42|PREEMPTED|10|8|cpu=8|0:15|2026-10-21T12:00:00|2026-10-21T12:00:01|2026-10-21T12:00:11|broker-attempt:{ATTEMPT}|/managed/original-attempt|\n"
    result = observation(row)
    assert result["terminal"] and result["state"] == "failed"
    assert result["accounting"] == {"final": True, "cpu_core_seconds": 80, "gpu_device_seconds": 0}
