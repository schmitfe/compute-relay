from pathlib import Path
import pytest
from compute_relay.backends import prepare
from compute_relay.backends.preparation import sha256
from compute_relay.backends.slurm import parse_observation
from compute_relay.remote import helper

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

def observation(row, *, gpus=0):
    identity = {"job_id": "42", "attempt_id": ATTEMPT, "submitted_at": "2026-10-21T12:00:00+00:00",
                "work_dir": "/managed/original-attempt"}
    return parse_observation(identity, {"returncode": 0, "stdout": ""},
                             {"returncode": 0, "stdout": row}, expected_gpus=gpus)

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
