"""The supervisor captures a sustained stall without restarting the service."""

import signal
from types import SimpleNamespace

from compute_relay import local_runtime


def test_watch_captures_once_per_sustained_outage_and_recovers(monkeypatch, capsys):
    health = [False, False, True, False, False, False, False, True, False, False, False]
    results = iter(health)
    waits, signals = [], []
    def wait(seconds):
        waits.append(seconds)
        return len(waits) > len(health)
    calls = []
    def check(*args):
        calls.append(args)
        return next(results)
    monkeypatch.setattr(local_runtime, "_health", check)
    process = SimpleNamespace(poll=lambda: None, send_signal=signals.append)
    local_runtime._watch_health(process, "http://127.0.0.1:8768", "installation", "instance",
        SimpleNamespace(wait=wait))
    assert signals == [signal.SIGUSR1, signal.SIGUSR1]
    assert waits == [15] * (len(health) + 1)
    assert calls == [("http://127.0.0.1:8768", "installation", "instance")] * len(health)
    assert capsys.readouterr().out.count("private web.log") == 2


def test_watch_does_not_signal_a_stopped_server(monkeypatch):
    def forbidden(*args):
        raise AssertionError("An exited child must not be probed or signalled")
    monkeypatch.setattr(local_runtime, "_health", forbidden)
    local_runtime._watch_health(SimpleNamespace(poll=lambda: 0, send_signal=forbidden),
        "http://127.0.0.1:8768", "installation", "instance", SimpleNamespace(wait=lambda _: False))


def test_watch_handles_server_exiting_before_signal(monkeypatch):
    waits, signals = [], []
    monkeypatch.setattr(local_runtime, "_health", lambda *args: False)
    def signal_exited(kind):
        signals.append(kind)
        raise ProcessLookupError
    local_runtime._watch_health(SimpleNamespace(poll=lambda: None, send_signal=signal_exited),
        "http://127.0.0.1:8768", "installation", "instance",
        SimpleNamespace(wait=lambda seconds: waits.append(seconds)))
    assert waits == [15] * 3 and signals == [signal.SIGUSR1]
