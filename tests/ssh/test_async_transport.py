"""First-party local subprocess fixtures; no network, keys or real Slurm jobs."""

import asyncio
import errno
import json
import os
from pathlib import Path
import sys
import time

import pytest

from compute_relay.transports.openssh import OpenSSHTransport
from compute_relay.transports.openssh import TransportError


def transport(timeout=10, **ssh):
    return OpenSSHTransport({"command_timeout_seconds": timeout, "ssh": {
        "host": "not-contacted", "user": "broker", "known_hosts": "/protected/known_hosts", **ssh,
    }})


def assert_process_stopped(pid, *, timeout=2):
    # Reaping the direct child does not wait for its descendants to finish dying.
    deadline = time.monotonic() + timeout
    while True:
        try:
            status = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        except (FileNotFoundError, ProcessLookupError):
            return
        state = status.split()[2]
        if state == "Z":
            return
        assert time.monotonic() < deadline, f"Process {pid} is still alive (state {state})"
        time.sleep(0.01)


@pytest.mark.parametrize("error", [FileNotFoundError(errno.ENOENT, "reaped"), ProcessLookupError(errno.ESRCH, "reaped")])
def test_process_stop_check_accepts_reaping_during_the_single_read(monkeypatch, error):
    reads = []
    def read(path, **kwargs):
        reads.append(path)
        raise error
    monkeypatch.setattr(Path, "read_text", read)
    assert_process_stopped(123)
    assert reads == [Path("/proc/123/stat")]


@pytest.mark.parametrize("state", ["Z", "R", "S", "D"])
def test_process_stop_check_accepts_zombie_but_rejects_running_process(monkeypatch, state):
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: f"123 (python) {state} 1 2 3")
    if state == "Z":
        assert_process_stopped(123)
    else:
        with pytest.raises(AssertionError):
            assert_process_stopped(123, timeout=0)


@pytest.mark.parametrize("final_state", ["Z", FileNotFoundError(), ProcessLookupError()])
def test_process_stop_check_waits_for_pending_kill(monkeypatch, final_state):
    states = iter(["R", final_state])
    def read(*_args, **_kwargs):
        state = next(states)
        if isinstance(state, OSError):
            raise state
        return f"123 (python) {state} 1 2 3"
    monkeypatch.setattr(Path, "read_text", read)
    assert_process_stopped(123)


@pytest.mark.parametrize("error", [PermissionError(errno.EACCES, "denied"), OSError(errno.EIO, "failed")])
def test_process_stop_check_does_not_hide_unrelated_read_errors(monkeypatch, error):
    def read(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(type(error)) as raised:
        assert_process_stopped(123)
    assert raised.value is error


def test_async_streaming_is_bidirectional_and_does_not_block_the_event_loop():
    async def scenario():
        payload = b"uploaded source is data only\n" * 100000
        # Emit more than a pipe buffer before consuming input: writing all input
        # synchronously before reading output would deadlock here.
        program = "import sys,json; sys.stdout.write(' '*100000); sys.stdout.flush(); data=sys.stdin.buffer.read(); print(json.dumps({'bytes':len(data)}))"
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            for _ in range(10):
                ticks += 1
                await asyncio.sleep(0.001)

        result, _ = await asyncio.gather(transport()._run([sys.executable, "-I", "-c", program], payload), heartbeat())
        assert json.loads(result) == {"bytes": len(payload)} and ticks == 10
    asyncio.run(scenario())


@pytest.mark.parametrize("stream,limit", [("stdout", "STDOUT_LIMIT"), ("stderr", "STDERR_LIMIT")])
def test_async_output_flood_is_bounded_sanitized_and_reaped(stream, limit, monkeypatch):
    async def scenario():
        item = transport()
        setattr(item, limit, 8192)
        spawned = []
        create = asyncio.create_subprocess_exec

        async def capture(*args, **kwargs):
            process = await create(*args, **kwargs)
            spawned.append(process)
            assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C"}
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
        with pytest.raises(TransportError, match="limit") as error:
            await item._run([sys.executable, "-I", "-c", f"import sys; sys.{stream}.write('private-diagnostic'*10000)"], b"")
        assert "private-diagnostic" not in str(error.value)
        assert spawned[0].returncode is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(spawned[0].pid, os.WNOHANG)
    asyncio.run(scenario())


def test_async_total_timeout_includes_input_backpressure_and_reaps_child(monkeypatch):
    async def scenario():
        spawned = []
        create = asyncio.create_subprocess_exec

        async def capture(*args, **kwargs):
            process = await create(*args, **kwargs)
            spawned.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
        with pytest.raises(TransportError, match="timed out"):
            await transport(timeout=1)._run([sys.executable, "-I", "-c", "import time; time.sleep(30)"], b"x" * 4000000)
        assert spawned[0].returncode is not None
    asyncio.run(scenario())


def test_cancellation_during_process_creation_still_reaps_child(monkeypatch):
    async def scenario():
        spawned, created, release = [], asyncio.Event(), asyncio.Event()
        create = asyncio.create_subprocess_exec

        async def delayed(*args, **kwargs):
            process = await create(*args, **kwargs)
            spawned.append(process)
            created.set()
            await release.wait()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
        call = asyncio.create_task(transport()._run([sys.executable, "-I", "-c", "import time; time.sleep(30)"], b""))
        await created.wait()
        call.cancel()
        await asyncio.sleep(0)
        call.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert spawned[0].returncode is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(spawned[0].pid, os.WNOHANG)
    asyncio.run(scenario())


def test_cancellation_stops_pipe_inheriting_descendants(tmp_path):
    async def scenario():
        child_pid = tmp_path / "descendant"
        program = ("import os,time,pathlib\n"
                   "pid=os.fork()\n"
                   f"pathlib.Path({str(child_pid)!r}).write_text(str(pid)) if pid else None\n"
                   "time.sleep(30)\n")
        call = asyncio.create_task(transport()._run([sys.executable, "-I", "-c", program], b""))
        for _ in range(200):
            if child_pid.exists():
                break
            await asyncio.sleep(0.005)
        assert child_pid.exists()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, timeout=2)
        # A killed descendant may briefly remain an orphan zombie until init
        # reaps it. It must no longer run or hold this operation's pipes open.
        pid = int(child_pid.read_text())
        assert_process_stopped(pid)
    asyncio.run(scenario())


def test_async_and_sync_share_pinned_identity_and_dead_master_fail_closed_argv():
    item = transport()
    argv = item.argv()
    for option in ("IdentityFile=none", "IdentityAgent=none", "IdentitiesOnly=yes", "ControlPath=none", "StrictHostKeyChecking=yes", "ForwardAgent=no"):
        assert option in argv
    argv = transport(mode="mfa", control_path="/protected/master").argv()
    assert "ControlPath=/protected/master" in argv and "ProxyCommand=/bin/false" in argv and "ProxyJump=none" in argv


def test_async_helper_validation_does_not_spawn_for_oversized_or_invalid_operations(monkeypatch):
    async def scenario():
        async def forbidden(*_args, **_kwargs):
            pytest.fail("Invalid request reached the subprocess layer")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
        item = transport()
        item.PAYLOAD_LIMIT = 4
        with pytest.raises(TransportError, match="payload"):
            await item.call_helper("fixed source", {"operation": "discover"})
        item.connection["python_path"] = "python3"
        with pytest.raises(TransportError, match="interpreter"):
            await item.call_helper("fixed source", {})
    asyncio.run(scenario())
