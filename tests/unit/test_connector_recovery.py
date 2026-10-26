"""Outages must not cause login storms, lost intent or overlapping probes."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from compute_relay.transports.openssh import TransportError
from compute_relay.worker.recovery import RetryGate, SessionRecovery, reachable
from compute_relay.worker import personal_connector


def test_retry_gate_allows_only_one_recovery_probe_and_ignores_stale_success():
    async def scenario():
        clock = [0]
        gate = RetryGate(clock=lambda: clock[0], jitter=lambda: 1)
        old_epoch = await gate.enter()
        await gate.failed()
        await gate.succeeded(old_epoch)
        assert gate.failures == 1  # An older in-flight reply cannot clear the outage.
        clock[0] = 100
        tasks = [asyncio.create_task(gate.enter()) for _ in range(4)]
        try:
            await asyncio.sleep(.01)
            assert sum(task.done() for task in tasks) == 1
            epoch = next(task.result() for task in tasks if task.done())
            await gate.succeeded(epoch)
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
            assert gate.failures == 0 and not gate.probing
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())


def test_retry_backoff_is_capped_and_waiting_is_cancellable():
    async def scenario():
        gate = RetryGate(jitter=lambda: 1.2)
        for _ in range(20):
            await gate.failed()
        assert 59 < gate.ready_at - gate.clock() <= 60
        task = asyncio.create_task(gate.enter())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not gate.probing
    asyncio.run(scenario())


def fixture(*, available_after=0, reject=False):
    now, delays, probes, messages, starts = [0], [], [], [], []
    async def sleep(seconds):
        delays.append(seconds); now[0] += seconds
    async def probe(host, port):
        probes.append((host, port))
        return len(probes) > available_after
    async def retire():
        return True
    async def reconnect():
        starts.append(now[0])
        if reject:
            raise TransportError("private diagnostic must not be logged")
    session = SimpleNamespace(connection={"ssh": {"host": "cluster.invalid", "port": 2222}},
        retire_async=retire, reconnect_async=reconnect)
    status = SimpleNamespace(problem=messages.append, notice=messages.append, progress=messages.append)
    recovery = SessionRecovery(probe=probe, sleep=sleep, clock=lambda: now[0], jitter=lambda: 1)
    return SimpleNamespace(session=session, status=status, recovery=recovery, delays=delays,
        probes=probes, starts=starts, messages=messages)


def test_network_outage_backs_off_without_sending_credentials_then_stops():
    c = fixture(available_after=100)
    assert asyncio.run(c.recovery.reconnect(c.session, c.status)) is False
    assert c.delays[:6] == [2, 4, 8, 16, 32, 60]
    assert len(c.probes) == 12 and not c.starts and c.recovery.blocked
    assert all(probe == ("cluster.invalid", 2222) for probe in c.probes)
    assert asyncio.run(c.recovery.reconnect(c.session, c.status)) is False
    assert len(c.probes) == 12


def test_network_recovery_attempts_one_key_authentication_after_reachability():
    c = fixture(available_after=2)
    assert asyncio.run(c.recovery.reconnect(c.session, c.status)) is True
    assert c.delays == [2, 4, 8] and len(c.starts) == 1
    assert "restored" in c.messages[-1]


def test_failed_handshake_or_authentication_opens_circuit_until_explicit_restart():
    c = fixture(reject=True)
    assert asyncio.run(c.recovery.reconnect(c.session, c.status)) is False
    assert asyncio.run(c.recovery.reconnect(c.session, c.status)) is False
    assert len(c.starts) == len(c.probes) == 1 and c.recovery.blocked
    assert "private diagnostic" not in " ".join(c.messages)


def test_flapping_connections_have_a_cooldown_and_lifetime_authentication_cap():
    c = fixture()
    for _ in range(3):
        assert asyncio.run(c.recovery.reconnect(c.session, c.status)) is True
    assert asyncio.run(c.recovery.reconnect(c.session, c.status)) is False
    assert len(c.starts) == len(c.probes) == 3
    assert all(later - earlier >= 60 for earlier, later in zip(c.starts, c.starts[1:]))


def test_reachability_probe_sends_no_ssh_authentication(monkeypatch):
    events = []
    class Writer:
        def close(self):
            events.append("closed")
        async def wait_closed(self):
            events.append("drained")
        def write(self, _):
            pytest.fail("Network reachability probe sent application bytes")
    async def connect(host, port):
        assert (host, port) == ("cluster.invalid", 2222)
        return object(), Writer()
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert asyncio.run(reachable("cluster.invalid", 2222)) is True
    assert events == ["closed", "drained"]


def test_connector_resumes_same_state_only_after_session_operations_drained(monkeypatch, tmp_path):
    events = []
    state = {"session_seconds": 300}
    async def run(received, session, directory, **kwargs):
        assert received is state and directory == tmp_path
        events.append("drained" if not events else "resumed")
        return "session_lost" if len(events) == 1 else None
    async def recover(self, session, status):
        assert events == ["drained"]
        events.append("reconnected")
        return True
    async def close():
        events.append("closed")
    monkeypatch.setattr(personal_connector, "_serve_session", run)
    monkeypatch.setattr(SessionRecovery, "reconnect", recover)
    asyncio.run(personal_connector.serve(state, SimpleNamespace(close_async=close), tmp_path,
        deadline=time.monotonic() + 10))
    assert events == ["drained", "reconnected", "resumed", "closed"]


def test_recovery_does_not_extend_original_session_deadline(monkeypatch, tmp_path, capsys):
    closed = []
    async def lost(*args, **kwargs):
        return "session_lost"
    async def blocked(*args):
        await asyncio.Event().wait()
    async def close():
        closed.append(True)
    monkeypatch.setattr(personal_connector, "_serve_session", lost)
    monkeypatch.setattr(SessionRecovery, "reconnect", blocked)
    asyncio.run(personal_connector.serve({"session_seconds": 300}, SimpleNamespace(close_async=close), tmp_path,
        deadline=time.monotonic() + .02))
    assert closed == [True] and "lifetime reached during recovery" in capsys.readouterr().err


def test_revocation_never_triggers_connection_recovery(monkeypatch, tmp_path):
    async def revoked(*args, **kwargs):
        return "revoked"
    async def forbidden(*args):
        pytest.fail("Revoked broker identity caused another SSH login")
    async def close():
        pass
    monkeypatch.setattr(personal_connector, "_serve_session", revoked)
    monkeypatch.setattr(SessionRecovery, "reconnect", forbidden)
    asyncio.run(personal_connector.serve({"session_seconds": 300}, SimpleNamespace(close_async=close), tmp_path))
