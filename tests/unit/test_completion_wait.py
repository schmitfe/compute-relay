"""Bounded waits must release capacity and recheck authority on every read."""

import pytest

from compute_relay.control import completion
from compute_relay.control.errors import BrokerError


@pytest.fixture
def clock(monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(completion.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(completion.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    return elapsed


def test_wait_returns_only_when_changed_or_bounded_timeout(clock):
    old = completion.revision({"state": "prepared"})
    reads = []
    def read():
        reads.append(clock[0])
        return {"state": "done" if clock[0] >= 1 else "prepared"}
    result = completion.wait_for_snapshot(read, old, 20)
    assert result["changed"] and result["state"] == "done" and clock[0] == 1
    result = completion.wait_for_snapshot(lambda: {"state": "prepared"}, old, 2)
    assert not result["changed"] and result["revision"] == old and clock[0] == 3


def test_wait_rechecks_authority_and_releases_its_slot(clock):
    calls = []
    def read():
        calls.append(True)
        if len(calls) > 1:
            raise BrokerError("SCOPE_DENIED", "Revoked", 403)
        return {"state": "prepared"}
    with pytest.raises(BrokerError, match="Revoked"):
        completion.wait_for_snapshot(read, completion.revision({"state": "prepared"}), 20)
    assert len(calls) == 2
    assert completion._wait_slots.acquire(blocking=False)
    assert completion._wait_slots.acquire(blocking=False)
    try:
        # A third waiter cannot consume the remaining HTTP worker capacity.
        with pytest.raises(BrokerError) as error:
            completion.wait_for_snapshot(lambda: {}, "a" * 64, 1)
        assert error.value.code == "WAIT_BUSY"
        assert completion.wait_for_snapshot(lambda: {"immediate": True})["changed"]
    finally:
        completion._wait_slots.release(); completion._wait_slots.release()


@pytest.mark.parametrize("token,seconds", [(None, 1), ("bad", 0), ("A" * 64, 1), ("a" * 64, 21), ("a" * 64, True), ("a" * 64, -1)])
def test_invalid_wait_does_not_read(token, seconds):
    with pytest.raises(BrokerError) as error:
        completion.wait_for_snapshot(lambda: pytest.fail("invalid wait read state"), token, seconds)
    assert error.value.code == "INVALID_WAIT"
