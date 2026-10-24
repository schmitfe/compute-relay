"""The local process/one-shot protocol boundary, without real keys or a cluster."""

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import sys

import pytest

from compute_relay.backends import prepare
from compute_relay.backends.preparation import helper_source, sha256
from compute_relay.transports import OpenSSHTransport, TransportError


def transport(timeout=10):
    return OpenSSHTransport({"command_timeout_seconds": timeout, "ssh": {
        "host": "not-contacted", "user": "broker", "known_hosts": "/protected/known_hosts",
    }})


def test_local_process_streams_large_input_without_shell_execution():
    # First-party harness reads bytes as data; it never evaluates the payload.
    payload = b"print('must not execute')\n" * 50000
    program = "import sys,json; data=sys.stdin.buffer.read(); print(json.dumps({'bytes':len(data)}))"
    output = transport()._run([sys.executable, "-I", "-c", program], payload)
    assert json.loads(output) == {"bytes": len(payload)}


def test_excess_diagnostics_are_killed_and_not_exposed():
    with pytest.raises(TransportError, match="diagnostic limit") as error:
        transport()._run([sys.executable, "-I", "-c", "import sys; sys.stderr.write('secret' * 30000)"], b"")
    assert "secret" not in str(error.value)


def test_remote_timeout_is_explicit_and_reaps_process():
    with pytest.raises(TransportError, match="timed out"):
        transport(timeout=1)._run([sys.executable, "-I", "-c", "import time; time.sleep(10)"], b"")


def test_one_shot_helper_has_a_durable_cross_process_submission_journal(tmp_path):
    calls = tmp_path / "calls"
    sbatch = tmp_path / "sbatch"
    # This is a fixed test scheduler, not a submitted user program. It only
    # acknowledges receipt and deliberately never executes the batch script.
    sbatch.write_text(f"#!/bin/sh\nprintf 'called\\n' >> {str(calls)!r}\nprintf '42;fixture\\n'\n")
    sbatch.chmod(0o700)
    content = b"raise RuntimeError('must never execute on the connector')\n"
    request = {"schema_version": "1.0", "project_id": "11111111-1111-4111-8111-111111111111",
               "connection_id": "22222222-2222-4222-8222-222222222222", "name": "stdio-test",
               "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
               "environment": {"id": "33333333-3333-4333-8333-333333333333", "revision_sha256": "a" * 64},
               "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
               "slurm": {"partition": "debug", "account": "broker"},
               "files": [{"path": "main.py", "sha256": sha256(content), "size_bytes": len(content), "executable": False}], "outputs": []}
    connection = {"backend": "fake", "revision_sha256": "f" * 64}
    prepared = prepare(request, {**request["environment"], "interpreter": "/usr/bin/python3"}, connection,
                       "44444444-4444-4444-8444-444444444444")
    files = {"main.py": content, **{path: source.encode() for path, source in prepared["generated_files"].items()}}
    operation = {"operation": "submit", "config": {"remote_root": str(tmp_path / "jobs"), "slurm_paths": {"sbatch": str(sbatch)}},
                 "attempt_id": "55555555-5555-4555-8555-555555555555", "manifest": prepared["manifest"],
                 "files": {path: base64.b64encode(data).decode() for path, data in files.items()}}

    def call(_):
        return json.loads(transport()._run([sys.executable, "-I", "-c", helper_source()], json.dumps(operation).encode()))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, range(2)))
    assert all(result["outcome"] == "submitted" for result in results)
    assert results[0]["native_identity"] == results[1]["native_identity"]
    assert calls.read_text().splitlines() == ["called"]
