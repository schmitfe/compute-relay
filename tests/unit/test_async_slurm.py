"""Awaitable Slurm adapter with a local fixed helper and stub scheduler only."""

import asyncio
import base64
import hashlib
import json
import sys
from uuid import uuid4

import pytest

from compute_relay.backends import canonical_json_bytes, prepare
from compute_relay.backends.async_slurm import AsyncSlurmBackend
from compute_relay.transports.async_openssh import AsyncOpenSSHTransport
from compute_relay.transports.openssh import TransportError
from compute_relay.worker.async_runner import AsyncWorker
from compute_relay.worker.journal import Journal


@pytest.fixture
def package(tmp_path):
    sentinel, calls = tmp_path / "source-must-not-run", tmp_path / "scheduler-calls"
    sbatch = tmp_path / "sbatch"
    sbatch.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nwith Path({str(calls)!r}).open('a') as out: out.write('called\\n')\nprint('42;fixture')\n")
    sbatch.chmod(0o700)
    source = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\n".encode()
    connection = {"backend": "slurm", "revision_sha256": "f" * 64, "remote_root": str(tmp_path / "jobs"),
                  "slurm_paths": {"sbatch": str(sbatch)}, "ssh": {"host": "not-contacted", "user": "broker", "known_hosts": "/protected/known_hosts"},
                  "site_profile": {"partitions": ["debug"], "accounts": ["broker"],}}
    request = {"schema_version": "1.0", "project_id": str(uuid4()), "connection_id": str(uuid4()), "name": "async-helper",
        "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
        "environment": {"id": str(uuid4()), "revision_sha256": "a" * 64},
        "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
        "slurm": {"partition": "debug", "account": "broker"}, "outputs": [],
        "files": [{"path": "main.py", "sha256": hashlib.sha256(source).hexdigest(), "size_bytes": len(source), "executable": False}]}
    prepared = prepare(request, {**request["environment"], "interpreter": "/usr/bin/python3"}, connection, str(uuid4()))
    files = {"main.py": source, **{path: value.encode() for path, value in prepared["generated_files"].items()}}
    return connection, prepared["manifest"], files, sentinel, calls


def test_async_adapter_uses_fixed_helper_and_cross_process_durable_submission_fence(package):
    async def scenario():
        connection, manifest, files, sentinel, calls = package
        transport = AsyncOpenSSHTransport({"command_timeout_seconds": 10, "ssh": {
            "host": "never-contacted", "user": "broker", "known_hosts": "/protected/known_hosts"}})

        class LocalFixedHelper:
            async def call_helper(self, source, request):
                # Only first-party remote helper code executes in this controlled
                # temporary tree. The uploaded job script is merely staged.
                output = await transport._run([sys.executable, "-I", "-c", source], json.dumps(request).encode())
                return json.loads(output)

        attempt = str(uuid4())
        backend = AsyncSlurmBackend(connection, transport=LocalFixedHelper())
        first, second = await asyncio.gather(backend.submit(attempt, manifest, files), backend.submit(attempt, manifest, files))
        assert first["outcome"] == second["outcome"] == "submitted"
        assert first["native_identity"] == second["native_identity"]
        assert calls.read_text().splitlines() == ["called"] and not sentinel.exists()
        assert (await backend.reconcile(attempt, manifest))["native_identity"] == first["native_identity"]
    asyncio.run(scenario())


def test_async_worker_default_backend_uses_actual_async_subprocess_path(package, tmp_path, monkeypatch):
    async def scenario():
        connection, manifest, files, sentinel, calls = package
        # This explicitly configured first-party shim substitutes for SSH only
        # in the temporary fixture. It invokes the fixed helper, never a job.
        ssh = tmp_path / "fixture-ssh"
        ssh.write_text(f"#!{sys.executable}\nimport os,shlex,sys\n"
                       "assert 'SSH_AUTH_SOCK' not in os.environ\n"
                       "assert 'IdentityFile=none' in sys.argv\n"
                       "command=shlex.split(sys.argv[-1])\nos.execv(command[0],command)\n")
        ssh.chmod(0o700)
        connection["ssh_path"] = str(ssh)
        monkeypatch.setenv("SSH_AUTH_SOCK", "/ambient/agent-must-not-be-used")
        control_calls = []

        class Control:
            async def request(self, method, path, body):
                control_calls.append((path, body))
                return {"authorized": True}

        connection_id = manifest["request"]["connection_id"]
        worker = AsyncWorker(Control(), {connection_id: connection}, Journal(tmp_path / "worker-journal"), owner_id=uuid4(), extensions=[])
        lease = {"id": str(uuid4()), "operation": "submit", "attempt_id": str(uuid4()), "generation": 1,
            "connection_id": connection_id, "prepared": {"manifest": manifest,
            "manifest_sha256": hashlib.sha256(canonical_json_bytes(manifest)).hexdigest(),
            "files": {path: base64.b64encode(content).decode() for path, content in files.items()}}}
        await worker.execute(lease)
        assert control_calls[-1][1]["outcome"] == "submitted"
        assert control_calls[-1][1]["native_identity"]["job_id"] == "42"
        assert calls.read_text().splitlines() == ["called"] and not sentinel.exists()
    asyncio.run(scenario())


def test_async_submission_transport_failure_is_uncertain_without_retry_and_cancel_propagates(package):
    async def scenario():
        connection, manifest, files, *_ = package

        class Failure:
            def __init__(self): self.calls = 0
            async def call_helper(self, source, request):
                self.calls += 1
                raise TransportError("private path")

        failure = Failure()
        backend = AsyncSlurmBackend(connection, transport=failure)
        assert (await backend.submit(str(uuid4()), manifest, files))["outcome"] == "submission_uncertain"
        assert failure.calls == 1
        assert (await backend.observe({"job_id": "42"}))["accounting"] == {"final": False}

        class Cancelled:
            async def call_helper(self, source, request):
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await AsyncSlurmBackend(connection, transport=Cancelled()).submit(str(uuid4()), manifest, files)
    asyncio.run(scenario())


def test_async_collect_validates_paths_and_decodes_only_structured_artifacts(package):
    async def scenario():
        calls = []

        class Transport:
            async def call_helper(self, source, request):
                calls.append(request)
                assert "identity_file" not in request["config"]
                return {"stdout": "result", "artifacts_base64": {"results/metrics.json": base64.b64encode(b"{}").decode()}}

        backend = AsyncSlurmBackend(package[0], transport=Transport())
        with pytest.raises(ValueError):
            await backend.collect({"job_id": "42"}, outputs=["../private"])
        assert calls == []
        result = await backend.collect({"job_id": "42"}, outputs=["results/metrics.json"])
        assert result["artifacts"] == {"results/metrics.json": b"{}"} and calls[0]["operation"] == "collect"
    asyncio.run(scenario())
