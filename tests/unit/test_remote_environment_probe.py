"""Selected runtime inspection is bounded metadata, never interpreter execution."""

import json
import os
from types import SimpleNamespace

import pytest

from compute_relay.backends.environments import render_environment
from compute_relay.control.environment_inspection import validate_result
from compute_relay.remote import environment_probe as remote
from compute_relay.remote.bundles import bundle_source


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 99\n")
    path.chmod(0o700)
    return str(path)


def test_inspection_resolves_selected_paths_conda_registry_and_poetry_metadata(tmp_path, monkeypatch):
    home = tmp_path / "home"
    python = executable(tmp_path / "miniforge/bin/python3")
    conda = executable(tmp_path / "miniforge/bin/conda")
    poetry = executable(tmp_path / "tools/bin/poetry")
    prefix = tmp_path / "results with spaces/analysis"
    executable(prefix / "bin/python")
    (prefix / "conda-meta").mkdir()
    registry = home / ".conda/environments.txt"; registry.parent.mkdir(parents=True)
    registry.write_text(str(prefix) + "\n")
    cached = home / ".cache/pypoetry/virtualenvs/demo-abc-py3.12"
    executable(cached / "bin/python")
    monkeypatch.setattr(remote.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home)))
    calls = []
    def query(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "/bin/bash":
            assert "module load python3/miniforge3-24.3-py3.12" in argv[-1]
            return {"returncode": 0, "truncated": False, "stdout": "module noise\x00RELAY_ENV\x00PATH=" + str(tmp_path / "miniforge/bin") + ":" + str(tmp_path / "tools/bin") + "\x00SSH_AUTH_SOCK=secret\x00"}
        assert argv == [conda, "info", "--envs", "--json"]
        assert "HOME" not in kwargs["extra_env"] and "SSH_AUTH_SOCK" not in kwargs["extra_env"]
        return {"returncode": 0, "truncated": False, "stdout": json.dumps({"envs": [str(prefix)]})}
    monkeypatch.setattr(remote, "software_command", query)
    result = remote.inspect_environment({}, {"environment": {"modules": ["python3/miniforge3-24.3-py3.12"]}})
    validate_result(result)
    assert result["python"] == [python]
    assert result["conda_executable"] == conda and result["poetry_executable"] == poetry
    assert result["environments"] == [
        {"kind": "conda", "prefix": str(prefix), "interpreter": str(prefix / "bin/python")},
        {"kind": "poetry", "prefix": str(cached), "interpreter": str(cached / "bin/python")}]
    assert len(calls) == 2  # None of the sentinel interpreters/Poetry ran.


@pytest.mark.parametrize("selection", [
    {"modules": ["python3/3.12; touch /tmp/evil"]}, {"module_init": "/home/user/start.sh"},
    {"module_paths": ["/tmp/x:/evil"]}, {"shell": "echo arbitrary"},
    {"conda_executable": "/opt/not-conda"}, {"interpreter": "/opt/bin/anything"},
    {"prefix": "/tmp/a/../b"},
])
def test_invalid_inspection_never_starts_a_subprocess(selection, monkeypatch):
    monkeypatch.setattr(remote, "software_command", lambda *a, **k: pytest.fail("Invalid request executed"))
    with pytest.raises(ValueError):
        remote.inspect_environment({}, {"environment": selection})


def test_module_failure_returns_a_bounded_diagnostic_without_private_output(monkeypatch):
    monkeypatch.setattr(remote, "software_command", lambda *a, **k: {"returncode": 1, "truncated": False,
        "stdout": "private startup text", "stderr": "private paths"})
    assert remote.inspect_environment({}, {"environment": {"modules": ["missing/1"]}}) == {
        "ok": False, "error_code": "ENVIRONMENT_MODULE_LOAD_FAILED"}


@pytest.mark.parametrize("kind", ["venv", "poetry", "conda"])
def test_virtual_environment_can_keep_approved_module_runtime(kind):
    environment = {"kind": kind, "modules": ["gcc/14.1.0", "python3/3.12.4"],
        "module_paths": ["/opt/apps/modules/Software"], "prefix": "/scratch/my project/env",
        "interpreter": "/scratch/my project/env/bin/python"}
    if kind == "conda":
        environment["conda_executable"] = "/opt/miniforge/bin/conda"
    wrapper, command = render_environment(environment, "python3")
    assert "module load gcc/14.1.0 python3/3.12.4" in wrapper
    assert "/scratch/my project/env/bin/python" == command[-1]
    assert "poetry run" not in wrapper


def test_inspection_bundle_contains_no_job_dispatch_and_declares_all_dependencies():
    source = bundle_source("environment")
    assert "def submit(" not in source and "def inspect_environment(" in source
    namespace = {"__name__": "synthetic"}
    exec(compile(source, "<environment-bundle>", "exec"), namespace)
    with pytest.raises(ValueError, match="Unsupported fixed environment operation"):
        namespace["execute"]({}, {"operation": "shell"})


def test_conda_json_probe_runs_isolated_and_does_not_execute_discovered_python(tmp_path, monkeypatch):
    from compute_relay.remote.probes import software_command
    home = tmp_path / "home"; home.mkdir()
    prefix = tmp_path / "env"
    python = executable(prefix / "bin/python")
    sentinel = tmp_path / "interpreter-was-executed"
    (prefix / "bin/python").write_text("#!/bin/sh\ntouch " + str(sentinel) + "\nexit 99\n")
    conda = tmp_path / "bin/conda"; conda.parent.mkdir()
    conda.write_text("#!/usr/bin/python3\nimport os,json,sys\n"
        "assert sys.argv[1:] == ['info','--envs','--json']\n"
        "assert os.environ['HOME'] == '/nonexistent'\n"
        "assert os.environ['CONDARC'] == '/dev/null'\n"
        "assert os.environ['CONDA_NO_PLUGINS'] == 'true'\n"
        "assert 'SSH_AUTH_SOCK' not in os.environ and 'PYTHONPATH' not in os.environ\n"
        + "print(json.dumps({'envs': [" + repr(str(prefix)) + "]}))\n")
    conda.chmod(0o700)
    monkeypatch.setattr(remote.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home)))
    def command(argv, **kwargs):
        if argv[0] == "/bin/bash":
            return {"returncode": 0, "truncated": False, "stdout": "\x00RELAY_ENV\x00PATH=" + str(conda.parent) + "\x00SSH_AUTH_SOCK=private\x00PYTHONPATH=/untrusted\x00"}
        return software_command(argv, **kwargs)
    monkeypatch.setattr(remote, "software_command", command)
    result = remote.inspect_environment({}, {"environment": {}})
    assert result["conda_status"] == "available"
    assert result["environments"] == [{"kind": "conda", "prefix": str(prefix), "interpreter": python}]
    assert not sentinel.exists()
    validate_result(result)
