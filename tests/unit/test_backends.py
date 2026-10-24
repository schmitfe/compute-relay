"""Backend contracts without cluster credentials or uploaded-code execution."""

import base64
import copy
import json
from pathlib import Path

import pytest
import rfc8785

from compute_relay.backends import (
    BackendError, FakeBackend, SlurmBackend, canonical_json_bytes, prepare, verify_package,
)
from compute_relay.backends.preparation import sha256
from compute_relay.backends.slurm import parse_observation
from compute_relay.backends.discovery import parse_discovery
from compute_relay.backends.environments import EnvironmentProfileError, render_environment, validate_environment_config
from compute_relay.remote import helper
from compute_relay.transports import OpenSSHTransport, TransportError


ATTEMPT = "55555555-5555-4555-8555-555555555555"
REVISION = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def sample(tmp_path):
    content = b"raise RuntimeError('uploaded source must not run in the worker')\n"
    request = {
        "schema_version": "1.0", "project_id": "11111111-1111-4111-8111-111111111111",
        "connection_id": "22222222-2222-4222-8222-222222222222", "name": "test-job",
        "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
        "environment": {"id": "33333333-3333-4333-8333-333333333333", "revision_sha256": "b" * 64},
        "resources": {"nodes": 1, "cpus": 2, "gpus": 0, "memory_mib": 512, "time_limit_seconds": 60},
        "slurm": {"partition": "debug", "account": "broker"},
        "files": [{"path": "main.py", "sha256": sha256(content), "size_bytes": len(content), "executable": False}],
        "outputs": ["results/metrics.json"],
    }
    environment = {**request["environment"], "interpreter": "/usr/bin/python3", "modules": []}
    connection = {"backend": "fake", "state_directory": str(tmp_path / "fake"), "revision_sha256": "f" * 64,
                  "site_profile": {"maximum_cpus": 2, "maximum_gpus": 0, "maximum_memory_mib": 1024}}
    return request, environment, connection, content


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
    result = backend.submit(ATTEMPT, prepared["manifest"], files)
    assert not marker.exists()
    restarted = FakeBackend(sample[2])
    assert restarted.reconcile(ATTEMPT, prepared["manifest"])["native_identity"] == result["native_identity"]
    assert restarted.observe(result["native_identity"])["accounting"] == {"final": True, "cpu_core_seconds": 2, "gpu_device_seconds": 0}
    collected = restarted.collect(result["native_identity"], ["results/metrics.json"])
    assert json.loads(collected["artifacts"]["results/metrics.json"])["executed_uploaded_code"] is False
    with pytest.raises(BackendError):
        restarted.collect(result["native_identity"], ["not-declared"])


def test_fake_cancellation_is_durable(sample):
    prepared, files = package(sample)
    backend = FakeBackend(sample[2])
    identity = backend.submit(ATTEMPT, prepared["manifest"], files)["native_identity"]
    assert backend.cancel(identity)["outcome"] == "cancel_requested"
    assert FakeBackend(sample[2]).observe(identity)["state"] == "cancelled"


def test_native_transport_timeout_does_not_resubmit(sample):
    class Unavailable:
        calls = 0

        def call_helper(self, source, request):
            self.calls += 1
            assert "ssh" not in request["config"]
            raise TransportError("Lost reply after Slurm may have accepted")

    prepared, files = package(sample)
    transport = Unavailable()
    backend = SlurmBackend(sample[2], transport=transport)
    assert backend.submit(ATTEMPT, prepared["manifest"], files)["outcome"] == "submission_uncertain"
    assert transport.calls == 1
    assert backend.reconcile(ATTEMPT, prepared["manifest"])["outcome"] == "submission_uncertain"
    assert transport.calls == 2


def observation(row, *, gpus=0):
    identity = {"job_id": "42", "attempt_id": ATTEMPT, "submitted_at": "2026-10-21T12:00:00+00:00",
                "work_dir": "/managed/original-attempt"}
    return parse_observation(identity, {"returncode": 0, "stdout": ""},
                             {"returncode": 0, "stdout": row}, expected_gpus=gpus)


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
    return {"operation": "submit", "config": {"remote_root": str(tmp_path / "remote")}, "attempt_id": ATTEMPT,
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
    reconcile = {"operation": "reconcile", "attempt_id": ATTEMPT, "manifest": remote_request["manifest"]}
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
    reconcile = {"operation": "reconcile", "attempt_id": ATTEMPT, "manifest": remote_request["manifest"]}
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
    reconcile = {"operation": "reconcile", "attempt_id": ATTEMPT, "manifest": remote_request["manifest"]}
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
        helper.execute(remote_request["config"], {"operation": "collect", "native_identity": result["native_identity"],
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
    result = helper.execute(remote_request["config"], {"operation": "cancel", "native_identity": receipt["native_identity"]})
    assert result["outcome"] == "unknown"
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
    result = helper.execute(remote_request["config"], {"operation": "cancel", "native_identity": receipt["native_identity"]})
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
    native = backend.submit(ATTEMPT, prepared["manifest"], files)["native_identity"]
    assert backend.observe(native)["accounting"] == {"final": True, "cpu_core_seconds": 2, "gpu_device_seconds": 0}


def test_modules_use_loaded_interpreter_and_bind_the_exact_wrapper(sample):
    request, environment, connection, _ = sample
    environment.update(kind="modules", interpreter="python3", modules=["Python/3.12.1", "CUDA/12.4"],
                       module_init="/usr/share/lmod/lmod/init/bash")
    prepared, files = package(sample)
    wrapper = prepared["generated_files"]["_broker/environment.sh"]
    assert "if ! command -v module" in wrapper
    assert "  . /usr/share/lmod/lmod/init/bash\nfi\n" in wrapper
    assert "module purge\nmodule load Python/3.12.1 CUDA/12.4\n" in wrapper
    assert prepared["generated_files"]["_broker/batch.sh"].startswith("#!/bin/bash -l\n")
    assert "exec python3 ./main.py\n" in prepared["generated_files"]["_broker/batch.sh"]
    verify_package(prepared["manifest"], prepared["manifest_sha256"], files)
    environment["modules"] = ["Python/3.13.0"]
    assert prepare(request, environment, connection, REVISION)["manifest_sha256"] != prepared["manifest_sha256"]


@pytest.mark.parametrize("change", [
    {"module_init": "/tmp/user-modules.sh"}, {"modules": ["Python/../bad"]},
    {"modules": ["Python/3.12;id"]}, {"modules": ["-t"]}, {"interpreter": "../python3"},
])
def test_modules_cannot_inject_shell_or_initialize_project_scripts(change):
    config = {"kind": "modules", "interpreter": "python3", "modules": ["Python/3.12"], **change}
    with pytest.raises(EnvironmentProfileError):
        validate_environment_config(config)


@pytest.mark.parametrize("kind", ["venv", "poetry"])
def test_existing_virtual_environments_use_their_interpreter_without_project_hooks(sample, kind):
    import shlex

    prefix = "/approved/envs/space and ' quote"
    sample[1].update(kind=kind, prefix=prefix, interpreter=prefix + "/bin/python")
    prepared, _ = package(sample)
    wrapper = prepared["generated_files"]["_broker/environment.sh"]
    assert "activate" not in wrapper
    assert "poetry run" not in wrapper
    assert shlex.split(wrapper.split("export VIRTUAL_ENV=", 1)[1].splitlines()[0]) == [prefix]
    command = prepared["generated_files"]["_broker/batch.sh"].split("exec ", 1)[1].strip()
    assert shlex.split(command) == [prefix + "/bin/python", "./main.py"]


def test_conda_execution_is_an_approved_prefix_command_in_the_compute_package(sample):
    import shlex

    sample[1].update(kind="conda", prefix="/opt/conda/envs/analysis", interpreter="/opt/conda/envs/analysis/bin/python",
                     conda_executable="/opt/conda/bin/conda")
    prepared, _ = package(sample)
    command = prepared["generated_files"]["_broker/batch.sh"].split("exec ", 1)[1].strip()
    assert shlex.split(command) == ["/opt/conda/bin/conda", "run", "--no-capture-output", "--prefix",
                                  "/opt/conda/envs/analysis", "/opt/conda/envs/analysis/bin/python", "./main.py"]
    assert "CONDA_NO_PLUGINS=true" in prepared["generated_files"]["_broker/environment.sh"]
    sample[1]["interpreter"] = "/usr/bin/python3"
    with pytest.raises(BackendError, match="selected prefix"):
        package(sample)


def test_metadata_discovery_never_executes_candidate_python_or_poetry(tmp_path, monkeypatch):
    root = tmp_path / "scoped-envs"
    prefix = root / "analysis"
    (prefix / "bin").mkdir(parents=True)
    marker = tmp_path / "unexpected-execution"
    executable = prefix / "bin" / "python"
    executable.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    executable.chmod(0o755)
    (prefix / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.2\n")
    # Never follow a sibling directory symlink into another project or home.
    (root / "outside-link").symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ())
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: pytest.fail("metadata discovery executed a command"))
    snapshot = helper.software_discovery({"environment_discovery": {"python_executables": [str(executable)],
        "search_roots": [{"path": str(root), "kind": "poetry", "max_depth": 2}]}})
    assert not marker.exists()
    assert snapshot["scope"] == "login_node" and snapshot["verified_on_compute"] is False
    assert snapshot["search"]["visited_directories"] == 2
    assert snapshot["environments"] == [{"kind": "poetry", "prefix": str(prefix), "interpreter": str(executable),
        "source": "configured_root_metadata", "verified_on_compute": False, "python_version": "3.12.2"}]


def test_software_probe_output_and_runtime_are_bounded():
    result = helper.software_command(["/bin/sh", "-c", "while :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done"], timeout=1)
    assert result["truncated"] is True
    assert len(result["stdout"].encode()) == 65536
    assert result["returncode"] != 0
    result = helper.software_command(["/bin/sleep", "10"], timeout=0.05)
    assert result["truncated"] is True


def test_module_terse_output_keeps_names_and_ignores_prose_and_directory_headers():
    text = "/opt/modulefiles:\nPython/\nPython/3.12(D)\nCUDA/12.4\ngcc\nSome explanatory prose\n../malicious\n"
    assert helper.available_modules(text) == ["CUDA/12.4", "Python/3.12", "gcc"]


@pytest.mark.parametrize("options", [
    {"shell": "id"}, {"module_init": "/tmp/modules.sh"},
    {"search_roots": [{"path": "/home", "kind": "venv"}]},
    {"search_roots": [{"path": "/opt/venvs", "max_depth": 20}]},
    {"python_executables": ["python3"]}, {"conda_executable": "/bin/bash"},
])
def test_discovery_rejects_unbounded_or_arbitrary_command_configuration(options):
    snapshot = helper.software_discovery({"environment_discovery": options})
    assert snapshot["status"] == "invalid_configuration"


def test_conda_inventory_is_fixed_and_does_not_use_personal_registries(tmp_path, monkeypatch):
    prefix = tmp_path / "conda-root" / "env1"
    (prefix / "bin").mkdir(parents=True)
    executable = prefix / "bin/python"
    executable.write_text("never executed")
    executable.chmod(0o755)
    calls = []

    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        return {"returncode": 0, "stdout": json.dumps({"envs": [str(prefix)]}), "stderr": "", "truncated": False}

    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ())
    monkeypatch.setattr(helper, "software_command", command)
    snapshot = helper.software_discovery({"environment_discovery": {"python_executables": [],
        "conda_executable": "/opt/conda/bin/conda", "search_roots": [{"path": str(prefix.parent), "kind": "conda"}]}})
    assert calls == [(["/opt/conda/bin/conda", "env", "list", "--json"], {"extra_env": {"CONDA_ENVS_PATH": str(prefix.parent)}})]
    assert snapshot["conda"]["prefixes"] == [str(prefix)]
    assert snapshot["environments"][0]["interpreter"] == str(executable)


def test_discovery_config_is_forwarded_without_ssh_credentials():
    class CapturingTransport:
        def call_helper(self, source, request):
            assert "identity_file" not in repr(request)
            assert request["config"]["environment_discovery"] == {"python_executables": []}
            return {"software": {"status": "available", "scope": "login_node", "verified_on_compute": False}}

    result = SlurmBackend({"ssh": {"identity_file": "/private/key"}, "environment_discovery": {"python_executables": []}},
                          transport=CapturingTransport()).discover()
    assert result["software"]["verified_on_compute"] is False


def test_environment_search_stops_at_its_directory_budget(tmp_path, monkeypatch):
    root = tmp_path / "many-envs"
    root.mkdir()
    for number in range(270):
        (root / str(number)).mkdir()
    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ())
    result = helper.software_discovery({"environment_discovery": {"python_executables": [],
        "search_roots": [{"path": str(root), "kind": "venv", "max_depth": 3}]}})
    assert result["search"]["visited_directories"] <= 256
    assert result["search"]["truncated"] is True


def test_software_probe_scrubs_personal_startup_and_conda_plugin_environment(monkeypatch):
    monkeypatch.setenv("HOME", "/secret/personal-home")
    monkeypatch.setenv("BASH_ENV", "/secret/startup")
    monkeypatch.setenv("PYTHONPATH", "/secret/plugin-path")
    result = helper.software_command(["/usr/bin/env"])
    env = dict(line.split("=", 1) for line in result["stdout"].splitlines())
    assert env["HOME"] == "/nonexistent"
    assert env["CONDA_NO_PLUGINS"] == "true"
    assert env["CONDARC"] == "/dev/null"
    assert "BASH_ENV" not in env and "PYTHONPATH" not in env


def test_default_module_paths_are_validated_and_supplied_to_the_fixed_probe(monkeypatch):
    observed = []
    trusted = {"/etc/modulefiles", "/usr/share/lmod/lmod/modulefiles"}

    def audit(path, budget):
        observed.append(path)
        if path not in trusted:
            raise FileNotFoundError(path)

    def command(argv, **kwargs):
        assert argv[:4] == ["/bin/bash", "--noprofile", "--norc", "-c"]
        assert kwargs["extra_env"]["MODULEPATH"] == "/etc/modulefiles:/usr/share/lmod/lmod/modulefiles"
        assert "export MODULEPATH=/etc/modulefiles:/usr/share/lmod/lmod/modulefiles; module --terse avail" in argv[4]
        return {"returncode": 0, "stdout": "Python/3.12\n", "stderr": "", "truncated": False}

    monkeypatch.setattr(helper, "trusted_module_tree", audit)
    monkeypatch.setattr(helper, "trusted_system_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ("/usr/share/lmod/lmod/init/bash",))
    monkeypatch.setattr(helper, "software_command", command)
    snapshot = helper.software_discovery({"environment_discovery": {"python_executables": []}})
    assert observed == list(helper.SYSTEM_MODULE_PATHS)
    assert snapshot["modules"]["available"] == ["Python/3.12"]
    assert snapshot["modules"]["module_paths"] == ["/etc/modulefiles", "/usr/share/lmod/lmod/modulefiles"]
    assert snapshot["modules"]["path_source"] == "automatic"


def test_explicit_module_paths_replace_defaults_and_preserve_configuration_order(monkeypatch):
    audited = []
    monkeypatch.setattr(helper, "trusted_module_tree", lambda path, _budget: audited.append(path))
    options = helper.discovery_config({"environment_discovery": {"module_paths": ["/apps/modules/Core", "/apps/modules/Linux"]}})
    paths, skipped = helper.effective_module_paths(options)
    assert paths == ["/apps/modules/Core", "/apps/modules/Linux"]
    assert audited == paths and skipped == []


def test_explicit_user_writable_module_path_is_rejected_without_executing_module_code(tmp_path, monkeypatch):
    unsafe = tmp_path / "modulefiles"
    unsafe.mkdir()
    (unsafe / ".modulerc.lua").write_text("must not execute")
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: pytest.fail("unsafe module code ran"))
    snapshot = helper.software_discovery({"environment_discovery": {"module_paths": [str(unsafe)]}})
    assert snapshot["status"] == "invalid_configuration"


def test_module_path_trust_check_rejects_writable_ancestors(monkeypatch):
    import stat
    from types import SimpleNamespace

    def info(path):
        return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | (0o777 if path == "/opt" else 0o755))

    monkeypatch.setattr(helper.os, "lstat", info)
    monkeypatch.setattr(helper.os, "stat", info)
    monkeypatch.setattr(helper.os.path, "realpath", lambda path: path)
    with pytest.raises(ValueError, match="root-owned and non-writable"):
        helper.trusted_system_path("/opt/modulefiles", directory=True)


def test_empty_safe_module_paths_report_that_site_configuration_is_needed(monkeypatch):
    monkeypatch.setattr(helper, "trusted_module_tree", lambda *_args: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(helper, "trusted_system_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ("/usr/share/lmod/lmod/init/bash",))
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: pytest.fail("module probe without safe paths"))
    snapshot = helper.software_discovery({"environment_discovery": {"python_executables": []}})
    assert snapshot["modules"]["status"] == "configuration_required"
    assert snapshot["modules"]["module_paths"] == []
    assert any("site module_paths" in note for note in snapshot["notes"])


def test_module_environment_binds_the_selected_path_before_and_after_initialization(sample):
    import shlex

    sample[1].update(kind="modules", interpreter="python3", modules=["Python/3.12"],
                     module_paths=["/apps/modulefiles/Core", "/apps/modulefiles/path with ' quote"], module_init="/etc/profile.d/modules.sh")
    prepared, files = package(sample)
    lines = prepared["generated_files"]["_broker/environment.sh"].splitlines()
    exports = [index for index, line in enumerate(lines) if line.startswith("export MODULEPATH=")]
    initialization = lines.index("  . /etc/profile.d/modules.sh")
    assert len(exports) == 2 and exports[0] < initialization < lines.index("module purge") < exports[1] < lines.index("module load Python/3.12")
    joined_path = ":".join(sample[1]["module_paths"])
    assert shlex.split(lines[exports[0]].split("=", 1)[1]) == [joined_path]
    verify_package(prepared["manifest"], prepared["manifest_sha256"], files)
    sample[1]["module_paths"] = ["/apps/different-modulefiles"]
    assert package(sample)[0]["manifest_sha256"] != prepared["manifest_sha256"]


@pytest.mark.parametrize("paths", [[], "bad", ["relative/path"], ["/apps/a:/home/user/modules"], ["/"], ["/apps/a\nunsafe"], ["/apps/a"] * 17])
def test_module_environment_rejects_invalid_path_lists(paths):
    with pytest.raises(EnvironmentProfileError):
        validate_environment_config({"kind": "modules", "interpreter": "python3", "module_paths": paths})


def test_nonmodule_profile_cannot_silently_ignore_module_path_selection():
    with pytest.raises(EnvironmentProfileError, match="requires a modules profile"):
        validate_environment_config({"kind": "system", "interpreter": "/usr/bin/python3", "module_paths": ["/apps/modules"]})


@pytest.fixture
def metadata_module_catalog(tmp_path, monkeypatch):
    """Real traversal with namespace-independent system/site owner metadata."""
    import os

    root = tmp_path / "modulefiles"
    root.mkdir(mode=0o755)
    real_fstat = helper.os.fstat
    root_inode = (root.stat().st_dev, root.stat().st_ino)
    system_tmp_inode = (Path("/tmp").stat().st_dev, Path("/tmp").stat().st_ino)

    def site_owned_root(fd):
        info = real_fstat(fd)
        fields = list(info)
        if (info.st_dev, info.st_ino) == root_inode:
            fields[4] = 1000004  # Non-root site ownership is permitted in this mode.
        if (info.st_dev, info.st_ino) == system_tmp_inode:
            fields[4] = 0  # The execution sandbox may remap host root to nobody.
        return os.stat_result(fields)

    monkeypatch.setattr(helper.os, "fstat", site_owned_root)
    previous_umask = os.umask(0o022)
    try:
        yield root
    finally:
        os.umask(previous_umask)


def test_metadata_module_catalog_never_reads_or_executes_module_files(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    (root / "python3").mkdir()
    (root / "gcc").mkdir()
    (root / "python3/3.12.2.lua").write_text("os.execute('must-not-execute')")
    (root / "gcc/13.2.tcl").write_text("exec must-not-execute")
    (root / "gcc/12.1").write_text("#%Module\nexec must-not-execute")
    (root / "julia.lua").write_text("error('must-not-read')")
    for name in (".modulerc.lua", "modulerc.lua", "aliases.tcl", "default", "README", "notes.txt", "setup.py"):
        (root / name).write_text("must-not-read")
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: pytest.fail("metadata invoked command"))
    monkeypatch.setattr(helper, "read_file", lambda *_args, **_kwargs: pytest.fail("metadata read file content"))
    monkeypatch.setattr(helper, "trusted_module_tree", lambda *_args, **_kwargs: pytest.fail("metadata used availability audit"))
    # Only initializer metadata is checked; the reference can prefill a recipe.
    monkeypatch.setattr(helper, "trusted_system_path", lambda *_args, **_kwargs: None)
    result = helper.software_discovery({"environment_discovery": {"module_inventory_mode": "metadata",
        "module_paths": [str(root)], "module_init": "/usr/share/lmod/lmod/init/bash", "python_executables": []}})
    modules = result["modules"]
    assert modules["status"] == "available"
    assert modules["available"] == ["gcc/12.1", "gcc/13.2", "julia", "python3/3.12.2"]
    assert modules["inventory_mode"] == "metadata" and modules["provisional"] is True
    assert modules["source"] == "configured_module_file_metadata"
    assert "were not evaluated" in modules["explanation"]
    assert modules["init_path"] == "/usr/share/lmod/lmod/init/bash"
    assert modules["initializer_status"] == "metadata_verified"


def test_metadata_catalog_skips_symlinks_and_writable_subtrees(metadata_module_catalog, tmp_path):
    root = metadata_module_catalog
    (root / "safe.lua").write_text("no reading")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.lua").write_text("must not inspect")
    (root / "directory_link").symlink_to(outside, target_is_directory=True)
    (root / "file_link.lua").symlink_to(outside / "private.lua")
    unsafe = root / "unsafe"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    (unsafe / "injected.lua").write_text("no reading")
    writable = root / "writable.lua"
    writable.write_text("no reading")
    writable.chmod(0o666)
    result = helper.metadata_module_inventory({"module_paths": [str(root)]})
    assert result["available"] == ["safe"]
    assert result["visited_directories"] == 1
    assert result["skipped"]["symlinks"] == 2
    assert result["skipped"]["unsafe_permissions"] == 2
    assert result["partial"] is True and result["warnings"]


def test_metadata_root_rejects_symlink_alias_even_when_target_is_readable(metadata_module_catalog, tmp_path):
    alias = tmp_path / "module-alias"
    alias.symlink_to(metadata_module_catalog, target_is_directory=True)
    result = helper.metadata_module_inventory({"module_paths": [str(alias)]})
    assert result["status"] == "unavailable" and result["module_paths"] == []
    assert result["roots"][0]["status"] == "unsafe_or_inaccessible"


def test_metadata_mode_rejects_writable_ancestor_without_opening_contents(tmp_path):
    parent = tmp_path / "shared-parent"
    parent.mkdir()
    parent.chmod(0o777)
    root = parent / "modulefiles"
    root.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="allows unsupported group/world writes"):
        helper.open_metadata_module_root(str(root))


@pytest.mark.parametrize("options", [
    {"module_inventory_mode": "metadata"}, {"module_inventory_mode": "metadata", "module_paths": []},
    {"module_inventory_mode": "shell"}, {"module_inventory_mode": []},
    {"module_inventory_mode": "metadata", "module_paths": ["relative/modules"]},
])
def test_metadata_configuration_requires_an_explicit_bounded_path_list(options):
    assert helper.software_discovery({"environment_discovery": options})["status"] == "invalid_configuration"


def test_default_module_inventory_keeps_availability_audit_and_semantics(monkeypatch):
    calls = []
    monkeypatch.setattr(helper, "effective_module_paths", lambda options: (calls.append(options["module_inventory_mode"]) or ([], [])))
    monkeypatch.setattr(helper, "metadata_module_inventory", lambda *_args: pytest.fail("default unexpectedly became metadata mode"))
    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ())
    result = helper.software_discovery({"environment_discovery": {"python_executables": []}})
    assert calls == ["availability"]
    assert result["modules"]["inventory_mode"] == "availability"
    assert result["modules"]["provisional"] is False


def test_metadata_catalog_bounds_names_and_leaves_later_roots_unopened(metadata_module_catalog, tmp_path, monkeypatch):
    root = metadata_module_catalog
    for number in range(520):
        (root / f"module{number}.lua").touch()
    later = tmp_path / "later-modules"
    later.mkdir()
    opened = []
    real_open = helper.open_metadata_module_root

    def record(path, **kwargs):
        opened.append(path)
        return real_open(path, **kwargs)

    monkeypatch.setattr(helper, "open_metadata_module_root", record)
    result = helper.metadata_module_inventory({"module_paths": [str(root), str(later)]})
    assert len(result["available"]) == 512
    assert result["truncated"] is True and result["partial"] is True
    assert result["roots"][1]["status"] == "not_scanned_limit"
    assert opened == [str(root)]


def test_metadata_catalog_checks_wallclock_inside_directory_iteration(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    (root / "a.lua").touch()
    (root / "b.lua").touch()
    clock = iter([0, 0, 0, 0, 0, 0, 0, 0, 3])
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(clock, 3))
    result = helper.metadata_module_inventory({"module_paths": [str(root)]})
    assert result["truncated"] is True
    assert result["visited_entries"] <= 1


def test_metadata_catalog_limits_entry_and_directory_counts(metadata_module_catalog):
    root = metadata_module_catalog
    # Housekeeping never populates the name set, so the entry bound is exercised.
    for number in range(4100):
        (root / f".ignored{number}").touch()
    result = helper.metadata_module_inventory({"module_paths": [str(root)]})
    assert result["visited_entries"] <= 4096
    assert result["truncated"] is True


@pytest.mark.parametrize("uid,mode,final,permitted,warned", [
    (0, 0o1777, False, True, True), (0, 0o775, False, True, True),
    (0, 0o755, True, True, False), (1000004, 0o755, True, True, False),
    (0, 0o777, False, False, False), (1000004, 0o775, False, False, False),
    (1000004, 0o1777, False, False, False), (0, 0o1777, True, False, False),
    (0, 0o775, True, False, False), (1000004, 0o775, True, False, False),
])
def test_metadata_shared_ancestor_exceptions_are_narrow_and_reported(uid, mode, final, permitted, warned):
    import stat
    from types import SimpleNamespace

    info = SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | mode)
    warnings = []
    if permitted:
        helper.metadata_module_directory_permissions(info, "/projects", final=final, ancestor_warnings=warnings)
        assert bool(warnings) is warned
        if warned:
            assert warnings[0]["path"] == "/projects"
            assert (warnings[0]["uid"], warnings[0]["mode"]) == (0, oct(mode))
    else:
        with pytest.raises(ValueError):
            helper.metadata_module_directory_permissions(info, "/projects", final=final, ancestor_warnings=warnings)
        assert warnings == []


def test_availability_mode_does_not_inherit_metadata_sticky_ancestor_exception(monkeypatch):
    import stat
    from types import SimpleNamespace

    info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o1777)
    monkeypatch.setattr(helper.os, "lstat", lambda *_args, **_kwargs: info)
    monkeypatch.setattr(helper.os.path, "realpath", lambda path: path)
    with pytest.raises(ValueError, match="root-owned and non-writable"):
        helper.trusted_system_path("/projects/modulefiles", directory=True)


def test_metadata_catalog_bounds_pending_handles_and_directory_depth(metadata_module_catalog):
    root = metadata_module_catalog
    for number in range(270):
        directory = root / f"directory{number}"
        directory.mkdir()
        (directory / "version.lua").touch()
    result = helper.metadata_module_inventory({"module_paths": [str(root)]})
    assert result["visited_directories"] <= 256
    assert result["truncated"] is True


def test_metadata_shared_ancestor_warning_evidence_is_bounded():
    import stat
    from types import SimpleNamespace

    info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o775)
    warnings = []
    for number in range(64):
        helper.metadata_module_directory_permissions(info, f"/shared/{number}", final=False, ancestor_warnings=warnings)
    with pytest.raises(ValueError, match="warning bound"):
        helper.metadata_module_directory_permissions(info, "/shared/extra", final=False, ancestor_warnings=warnings)
    assert len(warnings) == 64
