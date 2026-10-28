"""Personal setup state changes survive retries without controller imports."""

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from compute_relay.worker import personal_setup
from compute_relay.worker.client import ControlError
from compute_relay.worker.enrollment import profile_metadata, resolve_connection
from compute_relay.worker.journal import Journal
from compute_relay.worker.personal_connector import read_private, runtime_profile, write_private


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    state = {"owner_id": str(uuid4()), "registration_id": str(uuid4()), "connection_id": str(uuid4()),
        "broker_url": "https://broker.example", "worker_id": str(uuid4()), "worker_token": "cbw_synthetic-local-only",
        "session_seconds": 300, "module_discovery_mode": "auto", "site_profile": {"automatic_execution_allowed": False},
        "connection": {"backend": "slurm", "remote_root": "/home/researcher/jobs", "python_path": "/usr/bin/python3",
            "ssh": {"host": "cluster.example", "user": "researcher", "port": 22, "mode": "key",
                "identity_file": "/local/opaque-key-path", "known_hosts": "/local/known_hosts"}}}
    class Session:
        def runtime_configuration(self):
            result = deepcopy(state["connection"])
            result["ssh"].pop("identity_file", None)
            result["ssh"].update(mode="mfa", control_path="/local/private/s")
            return result
    class Client:
        def __init__(self):
            self.calls, self.queue, self.fail_report, self.deferred = [], [], False, False
        async def request(self, method, path, body):
            self.calls.append((path, deepcopy(body)))
            if path.endswith("/tasks"):
                return {"task": self.queue.pop(0) if self.queue else None, "defaults_deferred": self.deferred}
            if path.endswith("/report") and self.fail_report:
                self.fail_report = False
                raise ControlError("synthetic_secret_must_not_be_logged")
            return {"accepted": True}
    client, session = Client(), Session()
    alias, profile = runtime_profile(state, session)
    write_private(tmp_path / "state.json", state)
    worker = personal_setup.PersonalWorker(client, {}, Journal(tmp_path / "journal"), credential_profiles={alias: profile},
        extensions=[], owner_id=state["owner_id"], state=state, session=session, state_directory=tmp_path)
    async def remote(_session, operation, **arguments):
        if operation == "identity":
            return {"ok": True, "home": "/home/researcher", "cwd": "/home/researcher"}
        return {"ok": True, "path": arguments.get("path") or "/home/researcher", "writable": True, "exists": False}
    monkeypatch.setattr(personal_setup, "remote_setup", remote)
    return SimpleNamespace(state=state, session=session, client=client, worker=worker, directory=tmp_path)


def site():
    return {"partitions": ["cpu"], "accounts": [], "qos": [], "maximum_cpus": 4, "maximum_gpus": 0,
        "maximum_memory_mib": 4096, "maximum_time_limit_seconds": 600, "automatic_execution_allowed": False}


def task(kind="configure", **changes):
    return {"id": str(uuid4()), "lease_id": str(uuid4()), "generation": 1, "kind": kind,
        "request": {"remote_root": "/scratch/researcher/jobs", "site_profile": site()}, **changes}


def test_configuration_is_private_persistent_and_becomes_worker_scope(runtime):
    runtime.client.queue = [task()]
    asyncio.run(runtime.worker.setup_once())
    stored = read_private(runtime.directory / "state.json")
    assert stored["connection"]["remote_root"] == "/scratch/researcher/jobs"
    assert stored["site_profile"] == site() and stored["profile_generation"] == 2
    assert (runtime.directory / "state.json").stat().st_mode & 0o777 == 0o600
    body = runtime.client.calls[-1][1]
    assert body["configuration"]["remote_root"] == "/scratch/researcher/jobs"
    assert body["configuration"]["site_profile"] == site()
    assert "/local/opaque-key-path" not in str(body) and "cbw_synthetic" not in str(body)


def test_unsupported_configuration_fields_fail_without_changing_private_state(runtime):
    current = task()
    current["request"]["site_profile"]["cleanup_margin_seconds"] = 30
    runtime.client.queue = [current]
    before = read_private(runtime.directory / "state.json")
    asyncio.run(runtime.worker.setup_once())
    assert read_private(runtime.directory / "state.json") == before
    report = runtime.client.calls[-1][1]
    assert report["outcome"] == "failed" and "configuration" not in report




def test_lost_ack_replays_same_configuration_without_incrementing_again(runtime):
    current = task();runtime.client.queue = [current];runtime.client.fail_report = True
    with pytest.raises(ControlError):
        asyncio.run(runtime.worker.setup_once())
    first = read_private(runtime.directory / "state.json")
    runtime.client.queue = [{**current, "lease_id": str(uuid4()), "generation": 2}]
    asyncio.run(runtime.worker.setup_once())
    second = read_private(runtime.directory / "state.json")
    assert {key: value for key, value in first.items() if key != "pending_configuration_task"} == second
    assert second["last_configuration_task"] == current["id"]


def test_setup_authority_failure_keeps_core_worker_available(runtime, monkeypatch, capsys):
    async def forbidden(*args, **kwargs):
        raise ControlError("private-error-not-shown", 403)
    monkeypatch.setattr(runtime.client, "request", forbidden)
    asyncio.run(runtime.worker.hello())
    assert asyncio.run(runtime.worker.setup_once()) is False
    assert not runtime.worker._setup_authorized and runtime.worker._defer_defaults
    output = capsys.readouterr().err
    assert "Existing jobs" in output and "private-error" not in output


def test_replayed_configuration_uses_frozen_canonical_path(runtime, monkeypatch):
    current = task();runtime.client.queue = [current];runtime.client.fail_report = True
    with pytest.raises(ControlError):
        asyncio.run(runtime.worker.setup_once())
    async def moved_alias(*args, **kwargs):
        pytest.fail("A replay re-resolved a changed remote alias")
    monkeypatch.setattr(personal_setup, "remote_setup", moved_alias)
    runtime.client.queue = [{**current, "lease_id": str(uuid4()), "generation": 2}]
    asyncio.run(runtime.worker.setup_once())


def test_rejected_remote_path_does_not_change_local_profile(runtime, monkeypatch):
    async def unavailable(*args, **kwargs):
        return {"ok": False, "error_code": "ROOT_NOT_WRITABLE"}
    monkeypatch.setattr(personal_setup, "remote_setup", unavailable)
    before = read_private(runtime.directory / "state.json")
    runtime.client.queue = [task()]
    asyncio.run(runtime.worker.setup_once())
    assert read_private(runtime.directory / "state.json") == before
    assert runtime.client.calls[-1][1]["error_code"] == "ROOT_NOT_WRITABLE"


def test_module_avail_setting_is_persisted_published_and_replayable(runtime):
    current = task();current["request"]["module_availability_enabled"] = True
    runtime.client.queue = [current];runtime.client.fail_report = True
    with pytest.raises(ControlError):
        asyncio.run(runtime.worker.setup_once())
    stored = read_private(runtime.directory / "state.json")
    assert stored["module_availability_enabled"] is True
    assert runtime_profile(stored, runtime.session)[1]["environment_discovery"]["module_availability_enabled"] is True
    assert stored["last_configuration_result"]["module_availability_enabled"] is True
    runtime.client.queue = [{**current, "lease_id": str(uuid4()), "generation": 2}]
    asyncio.run(runtime.worker.setup_once())
    assert runtime.client.calls[-1][1]["result"]["module_availability_enabled"] is True
    disable = task();disable["request"]["module_availability_enabled"] = False
    runtime.client.queue = [disable]
    asyncio.run(runtime.worker.setup_once())
    assert not runtime.state["module_availability_enabled"]
    assert "module_availability_enabled" not in runtime_profile(runtime.state, runtime.session)[1]["environment_discovery"]


def test_default_root_is_identified_after_login_without_creating_it(runtime):
    assert asyncio.run(personal_setup.default_work_root(runtime.session)) == "/home/researcher/.compute-relay/jobs"


def test_discovery_initializes_maxima_once_then_preserves_user_configuration(runtime):
    snapshot = {"nodes": [{"name": "n1", "partition": "cpu", "cpus": "16", "memory_mib": "8192", "gres": "(null)"}],
        "partitions": [{"PartitionName": "cpu", "MaxTime": "00:20:00"}], "associations": []}
    class Backend:
        async def discover(self):
            return deepcopy(snapshot)
    runtime.worker.backend_factory = lambda _connection: Backend()
    alias = next(iter(runtime.worker.credential_profiles))
    configuration = resolve_connection({"backend": "slurm", "credential_profile": alias,
        "ssh": {"host": "cluster.example", "user": "researcher", "port": 22}, "remote_root": "/home/researcher/jobs"},
        runtime.worker.credential_profiles)[1]
    lease = {"connection_id": runtime.state["connection_id"], "id": str(uuid4()), "generation": 1, "configuration": configuration}
    asyncio.run(runtime.worker.discover(lease))
    stored = read_private(runtime.directory / "state.json")
    assert stored["site_profile"]["maximum_cpus"] == 16 and stored["defaults_initialized"]
    assert stored["site_profile"]["automatic_execution_allowed"] is False
    assert "cleanup_margin_seconds" not in stored["site_profile"]
    runtime.client.queue = [task()];asyncio.run(runtime.worker.setup_once())
    snapshot["nodes"][0]["cpus"] = "128"
    asyncio.run(runtime.worker.discover(lease))
    assert runtime.state["site_profile"]["maximum_cpus"] == 4


def test_hello_retains_work_from_response_instead_of_losing_lease(runtime):
    current = task("browse", request={"path": "/home/researcher"})
    runtime.client.queue = [current]
    asyncio.run(runtime.worker.hello())
    asyncio.run(runtime.worker.setup_once())
    reports = [body for path, body in runtime.client.calls if path.endswith("/report")]
    assert len(reports) == 1 and reports[0]["lease_id"] == current["lease_id"]


def test_active_job_upgrade_deferral_keeps_old_fingerprint(runtime):
    runtime.state.pop("module_discovery_mode")
    before = runtime_profile(runtime.state, runtime.session)[1]
    runtime.client.deferred = True
    asyncio.run(runtime.worker.hello())
    assert runtime_profile(runtime.state, runtime.session)[1] == before


def test_new_folder_retries_use_receipt_and_do_not_repeat_mutation(runtime, monkeypatch):
    operations = []
    async def remote(_session, operation, **arguments):
        operations.append(operation)
        result = {"ok": True, "path": "/home/researcher/new-project", "parent": "/home/researcher",
            "home": "/home/researcher", "entries": [], "writable": True, "truncated": False, "scanned_entries": 0}
        return {**result, "created": True} if operation == "mkdir" else result
    monkeypatch.setattr(personal_setup, "remote_setup", remote)
    current = task("mkdir", request={"path": "/home/researcher", "name": "new-project"})
    runtime.client.queue = [current];runtime.client.fail_report = True
    original_root = runtime.state["connection"]["remote_root"]
    with pytest.raises(ControlError):
        asyncio.run(runtime.worker.setup_once())
    stored = read_private(runtime.directory / "state.json")
    assert stored["last_folder_receipt"] == {"path": "/home/researcher/new-project", "created": True}
    assert "entries" not in stored["last_folder_receipt"]
    runtime.client.queue = [{**current, "lease_id": str(uuid4()), "generation": 2}]
    asyncio.run(runtime.worker.setup_once())
    assert operations == ["mkdir", "browse"]
    assert runtime.state["connection"]["remote_root"] == original_root
    assert runtime.client.calls[-1][1]["result"]["created"] is True


def test_verbose_setup_progress_does_not_print_paths_remote_errors_or_credentials(runtime, monkeypatch, capsys):
    from compute_relay.worker.connector_status import ConnectorStatus
    runtime.worker.status = ConnectorStatus(runtime.state, verbose=True)
    async def unavailable(*args, **kwargs):
        raise ValueError("remote-secret-response")
    monkeypatch.setattr(personal_setup, "remote_setup", unavailable)
    runtime.client.queue = [task("browse", request={"path": "/private/research/location"})]
    asyncio.run(runtime.worker.setup_once())
    output = capsys.readouterr().out
    assert "Listing folders" in output and "setup request failed" in output
    assert "/private/research/location" not in output and "remote-secret-response" not in output
    assert runtime.state["worker_token"] not in output and "/local/opaque-key-path" not in output


def test_verbose_job_progress_is_meaningful_without_repeating_observation_polls(runtime, monkeypatch, capsys):
    from compute_relay.worker.connector_status import ConnectorStatus
    runtime.worker.status = ConnectorStatus(runtime.state, verbose=True)
    operations = []
    async def execute(_worker, lease):
        operations.append(lease["operation"])
    monkeypatch.setattr(personal_setup.Worker, "_execute", execute)
    lease = {"operation": "observe", "attempt_id": str(uuid4()), "prepared": {"script": "private_script_body"}}
    asyncio.run(runtime.worker._execute(lease))
    asyncio.run(runtime.worker._execute(lease))
    asyncio.run(runtime.worker._execute({**lease, "operation": "submit"}))
    output = capsys.readouterr().out
    assert output.count("Tracking a job's Slurm status") == 1
    assert "approved job submission" in output and "reported to the broker" in output
    assert "private_script_body" not in output
    assert operations == ["observe", "observe", "submit"]


def test_non_verbose_job_progress_is_quiet(runtime, monkeypatch, capsys):
    async def execute(_worker, lease):
        pass
    monkeypatch.setattr(personal_setup.Worker, "_execute", execute)
    asyncio.run(runtime.worker._execute({"operation": "submit"}))
    assert capsys.readouterr().out == ""


def test_environment_inspection_reports_only_typed_result_without_changing_credentials(runtime, monkeypatch):
    selection = {"modules": ["python3/miniforge3-24.3-py3.12"]}
    runtime.client.queue = [task("environment_probe", request={"environment": selection})]
    before = read_private(runtime.directory / "state.json")
    async def inspect(session, requested):
        assert session is runtime.session and requested == selection
        return {"ok": True, "python": ["/opt/bin/python3"]}
    monkeypatch.setattr(personal_setup, "remote_environment", inspect)
    asyncio.run(runtime.worker.setup_once())
    assert runtime.client.calls[-1][1]["outcome"] == "succeeded"
    assert runtime.client.calls[-1][1]["result"] == {"ok": True, "python": ["/opt/bin/python3"]}
    assert read_private(runtime.directory / "state.json") == before


def test_configuration_waits_until_parallel_job_operations_have_drained(runtime):
    pending = task()
    runtime.client.queue = [pending]
    runtime.worker._in_progress["synthetic-active-attempt"] = "synthetic-active-lease"
    before = read_private(runtime.directory / "state.json")
    assert asyncio.run(runtime.worker.poll_once()) is False
    assert runtime.worker._pending_task == pending
    assert runtime.worker.configuration_pending
    assert not any(path == "/internal/v1/lease" for path, _ in runtime.client.calls)
    assert read_private(runtime.directory / "state.json") == before
    runtime.worker._in_progress.clear()
    assert asyncio.run(runtime.worker.setup_once()) is True
    assert not runtime.worker.configuration_pending
    assert read_private(runtime.directory / "state.json")["connection"]["remote_root"] == "/scratch/researcher/jobs"
