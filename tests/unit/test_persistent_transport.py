"""Actual local helper processes exercise stream framing, bounds and cleanup."""

import asyncio
from copy import deepcopy
import json
import struct
import sys
from uuid import uuid4
import base64
import subprocess

import pytest

from compute_relay.backends.slurm import SlurmBackend
from compute_relay.backends.preparation import helper_source
from compute_relay.remote.bundles import bundle_source
from compute_relay.transports.openssh import OpenSSHTransport, TransportError
from compute_relay.transports.persistent_openssh import PersistentHelperPool, PersistentOpenSSHTransport
from test_support.unit_test_remote_bundles import prepared, site


@pytest.fixture
def local_transport(site, monkeypatch):
    # Replace only the SSH process boundary. Installed stdlib helper code runs
    # in an isolated interpreter; uploaded project code is never executed.
    monkeypatch.setattr(OpenSSHTransport, "_helper_command", lambda self, source, request: ([sys.executable, "-I", "-c", source], b""))
    connection = deepcopy(site[0])
    connection["ssh"] = {"mode": "mfa", "host": "fixture.invalid", "user": "fixture", "port": 22,
        "known_hosts": "/synthetic/known_hosts", "control_path": "/synthetic/master"}
    return connection


def test_repeated_calls_reuse_one_process_and_keep_package_submission_once(site, local_transport):
    async def scenario():
        pool = PersistentHelperPool(4)
        backend = SlurmBackend(local_transport, transport=PersistentOpenSSHTransport(local_transport, pool))
        package, files = prepared(site)
        try:
            attempt = str(uuid4())
            native = (await backend.submit(attempt, package["manifest"], files))["native_identity"]
            for _ in range(8):
                assert (await backend.observe(native, manifest=package["manifest"]))["state"] == "running"
            assert pool.process_starts == 1 and pool.requests == 9
            batched = await backend.observe_many([{"native_identity": native, "manifest": package["manifest"]}])
            assert batched[attempt]["state"] == "running" and pool.process_starts == 1
            assert (await backend.reconcile(attempt, package["manifest"]))["native_identity"] == native
            assert site[4].read_text().splitlines().count("sbatch") == 1
            channels = tuple(pool.channels)
        finally:
            await pool.close()
        assert pool.pending == 0 and not pool.channels
        assert all(channel.process.returncode is not None for channel in channels)
    asyncio.run(scenario())


def test_persistent_startup_streams_bundle_before_framed_requests(site, tmp_path):
    # Emulate only ssh's final exec; retain the real command/bootstrap builder.
    ssh = tmp_path / "fixture-ssh"
    ssh.write_text(f"#!{sys.executable}\nimport os,shlex,sys\n"
        "command=shlex.split(sys.argv[-1])\nos.execv(command[0],command)\n")
    ssh.chmod(0o700)
    connection = {**deepcopy(site[0]), "ssh_path": str(ssh), "python_path": sys.executable,
        "command_timeout_seconds": 2, "ssh": {"mode": "mfa", "host": "fixture.invalid", "user": "fixture",
            "known_hosts": "/synthetic/known_hosts", "control_path": "/synthetic/master"}}
    async def scenario():
        pool = PersistentHelperPool(1)
        backend = SlurmBackend(connection, transport=PersistentOpenSSHTransport(connection, pool))
        package, files = prepared(site)
        try:
            result = await backend.submit(str(uuid4()), package["manifest"], files)
            assert result["outcome"] == "submitted"
            for _ in range(2):
                assert (await backend.observe(result["native_identity"], manifest=package["manifest"]))["state"] == "running"
            assert pool.process_starts == 1 and pool.requests == 3
        finally:
            await pool.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("reply", ["oversized", "wrong_id", "truncated"])
def test_invalid_frame_closes_channel_without_replaying_request(site, local_transport, reply):
    async def scenario():
        pool = PersistentHelperPool(1)
        body = ("sys.stdout.buffer.write(struct.pack('!I', 21*1024*1024));sys.stdout.buffer.flush()" if reply == "oversized" else
            "sys.stdout.buffer.write(struct.pack('!I', 10)+b'bad');sys.stdout.buffer.flush()" if reply == "truncated" else
            "out=json.dumps({'protocol':'compute-relay-stream/v1','id':'0'*32,'ok':True,'result':{}}).encode();sys.stdout.buffer.write(struct.pack('!I',len(out))+out);sys.stdout.buffer.flush()")
        pool.source = "import json,sys,struct\nn=struct.unpack('!I',sys.stdin.buffer.read(4))[0]\nsys.stdin.buffer.read(n)\n" + body + "\n"
        request = {"operation": "observe", "config": {k: local_transport[k] for k in ("remote_root", "slurm_paths")}}
        try:
            with pytest.raises(TransportError):
                await pool.call_helper(local_transport, helper_source("observe"), request)
            assert pool.requests == pool.process_starts == 1 and pool.pending == 0 and not pool.channels
        finally:
            await pool.close()
    asyncio.run(scenario())


def test_lost_submit_response_reconciles_without_second_scheduler_submission(site, local_transport):
    async def scenario():
        program = site[0]["slurm_paths"]["sbatch"]
        from pathlib import Path
        target = Path(program).resolve()
        source = target.read_text().replace("state.write_text(json.dumps(data))\n    if data.get('lose_reply')", "state.write_text(json.dumps(data))\n    import time; time.sleep(60)\n    if data.get('lose_reply')", 1)
        target.write_text(source)
        pool = PersistentHelperPool(1)
        connection = {**local_transport, "command_timeout_seconds": 1}
        backend = SlurmBackend(connection, transport=PersistentOpenSSHTransport(connection, pool))
        package, files = prepared(site)
        attempt = str(uuid4())
        try:
            result = await backend.submit(attempt, package["manifest"], files)
            assert result["outcome"] == "submission_uncertain"
            assert pool.pending == 0 and not pool.channels
            recovered = await backend.reconcile(attempt, package["manifest"])
            assert recovered["outcome"] == "submitted" and recovered["native_identity"]["job_id"] == "42"
            assert site[4].read_text().splitlines().count("sbatch") == 1
        finally:
            await pool.close()
    asyncio.run(scenario())


def test_cancellation_during_spawn_keeps_ownership_until_child_is_reaped(site, local_transport, monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        original = asyncio.create_subprocess_exec
        children = []
        async def delayed(*args, **kwargs):
            child = await original(*args, **kwargs)
            children.append(child); started.set()
            await release.wait()
            return child
        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
        pool = PersistentHelperPool(1)
        request = {"operation": "observe", "config": {k: local_transport[k] for k in ("remote_root", "slurm_paths")}}
        task = asyncio.create_task(pool.call_helper(local_transport, helper_source("observe"), request))
        await asyncio.wait_for(started.wait(), timeout=3)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool.pending == 0 and all(child.returncode is not None for child in children)
        await pool.close()
    asyncio.run(scenario())


def test_protected_source_and_configuration_checked_before_starting_process(local_transport):
    async def scenario():
        pool = PersistentHelperPool(1)
        try:
            with pytest.raises(TransportError):
                await pool.call_helper(local_transport, "arbitrary code", {"operation": "submit", "config": {}})
            with pytest.raises(TransportError):
                await pool.call_helper(local_transport, helper_source(), {"operation": "submit", "config": {"remote_root": "/another/root"}})
            assert pool.process_starts == pool.requests == 0
        finally:
            await pool.close()
    asyncio.run(scenario())


def test_stream_binds_configuration_before_accepting_another_submit(site):
    package, files = prepared(site)
    config = {k: site[0][k] for k in ("remote_root", "slurm_paths")}
    request = {"operation": "submit", "protocol": "slurm-jobs/v1", "config": config, "attempt_id": str(uuid4()),
        "manifest": package["manifest"], "files": {path: base64.b64encode(value).decode() for path, value in files.items()}}
    changed = {**deepcopy(request), "attempt_id": str(uuid4()), "config": {**config, "remote_root": config["remote_root"] + "-forbidden"}}
    frames = []
    for item in (request, changed):
        payload = json.dumps({"protocol": "compute-relay-stream/v1", "id": uuid4().hex, "request": item}).encode()
        frames.append(struct.pack("!I", len(payload)) + payload)
    result = subprocess.run([sys.executable, "-I", "-c", bundle_source("stream_jobs")], input=b"".join(frames), capture_output=True, timeout=10)
    assert result.returncode == 0
    replies, output = [], result.stdout
    while output:
        size = struct.unpack("!I", output[:4])[0]
        replies.append(json.loads(output[4:4 + size])); output = output[4 + size:]
    assert replies[0]["ok"] is True and replies[1]["ok"] is False
    from pathlib import Path
    assert not Path(changed["config"]["remote_root"]).exists()
    assert site[4].read_text().splitlines().count("sbatch") == 1


@pytest.mark.parametrize("payload", [
    struct.pack("!I", 151 * 1024 * 1024),
    b"\x00\x00",
    struct.pack("!I", 10) + b"short",
    struct.pack("!I", 2) + b"[]",
])
def test_stream_rejects_invalid_frames_before_any_operation(payload):
    result = subprocess.run([sys.executable, "-I", "-c", bundle_source("stream_jobs")], input=payload, capture_output=True, timeout=5)
    assert result.returncode != 0 and not result.stdout


def test_persistent_helper_transfers_large_files_and_live_logs_without_restarting(site, local_transport):
    from pathlib import Path
    import hashlib
    from compute_relay.backends import prepare
    from compute_relay.transfer import CHUNK_BYTES, FileReference

    async def scenario():
        connection, request, script, *_ = site
        data = b"input" * (CHUNK_BYTES // 2)
        request = deepcopy(request)
        request["files"].append({"path": "input.bin", "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data), "executable": False})
        package = prepare(request, {**request["environment"], "interpreter": "/usr/bin/python3"}, local_transport, str(uuid4()))
        files = {"main.py": script, "input.bin": FileReference(hashlib.sha256(data).hexdigest(), len(data)),
            **{path: text.encode() for path, text in package["generated_files"].items()}}
        pool = PersistentHelperPool(1)
        backend = SlurmBackend(local_transport, transport=PersistentOpenSSHTransport(local_transport, pool))
        attempt = str(uuid4())
        try:
            for offset in range(0, len(data), CHUNK_BYTES):
                chunk = data[offset:offset + CHUNK_BYTES]
                await backend.stage_input(attempt, package["manifest"], "input.bin", offset=offset,
                    content_base64=base64.b64encode(chunk).decode(), finish=offset + len(chunk) == len(data))
            native = (await backend.submit(attempt, package["manifest"], files))["native_identity"]
            directory = Path(native["work_dir"])
            output = b"result" * (2 * CHUNK_BYTES)
            (directory / "result.txt").write_bytes(output)
            (directory / "stdout.log").write_text("realization 1 done\n")
            logs = await backend.read_logs(native, manifest=package["manifest"], cursors={})
            assert base64.b64decode(logs["stdout"]["content_base64"]) == b"realization 1 done\n"
            inventory = await backend.output_manifest(native, manifest=package["manifest"])
            entry = inventory["files"][0]
            assert all(isinstance(part, str) for part in entry["version"])
            digest = hashlib.sha256()
            for offset in range(0, len(output), CHUNK_BYTES):
                chunk = await backend.output_chunk(native, manifest=package["manifest"], path="result.txt",
                    offset=offset, version=entry["version"])
                digest.update(base64.b64decode(chunk["content_base64"]))
            assert digest.hexdigest() == entry["sha256"] == hashlib.sha256(output).hexdigest()
            assert pool.process_starts == 1 and site[4].read_text().splitlines().count("sbatch") == 1
        finally:
            await pool.close()
    asyncio.run(scenario())
