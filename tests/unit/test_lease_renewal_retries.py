"""A short control outage must not interrupt work with a valid lease."""

import asyncio
from datetime import datetime, timedelta, timezone
import threading
import pytest

from compute_relay.worker import lease as module
from compute_relay.worker.client import ControlError


@pytest.fixture
def lease(monkeypatch):
    monkeypatch.setattr(module, "RENEWAL_INTERVAL_SECONDS", .005)
    monkeypatch.setattr(module, "RENEWAL_RETRY_SECONDS", .005, raising=False)
    monkeypatch.setattr(module, "RENEWAL_SAFETY_SECONDS", .005, raising=False)
    return {"id": "lease", "generation": 1, "attempt_id": "attempt",
        "lease_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=.15)).isoformat()}


def test_async_heartbeat_retries_one_http_blip(lease):
    async def scenario():
        renewed = asyncio.Event()
        class Client:
            calls = 0
            async def request(self, *args):
                self.calls += 1
                if self.calls == 1:
                    raise ControlError("CONTROL_UNAVAILABLE")
                renewed.set()
                return {"renewed": True}
        client = Client()
        async with module.renewing_async(client, lease):
            await asyncio.wait_for(renewed.wait(), .5)
        assert client.calls >= 2 and asyncio.current_task().cancelling() == 0
    asyncio.run(scenario())


def test_sync_heartbeat_retries_one_http_blip(lease):
    renewed = threading.Event()
    class Client:
        calls = 0
        def request(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise ControlError("CONTROL_UNAVAILABLE")
            renewed.set()
            return {"renewed": True}
    with module.renewing(Client(), lease):
        assert renewed.wait(.5)


def test_async_expiry_clears_only_its_own_cancellation(lease):
    lease["lease_expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=.04)).isoformat()
    async def scenario():
        class Client:
            async def request(self, *args):
                raise ControlError("CONTROL_UNAVAILABLE")
        owner = asyncio.current_task()
        with pytest.raises(ControlError, match="LEASE_RENEWAL_FAILED"):
            async with module.renewing_async(Client(), lease):
                await asyncio.sleep(.5)
        assert owner.cancelling() == 0
        await asyncio.sleep(0)
    asyncio.run(scenario())


def test_hung_async_renewal_is_bounded_by_remaining_lease(lease):
    lease["lease_expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=.04)).isoformat()
    async def scenario():
        class Client:
            async def request(self, *args):
                await asyncio.Event().wait()
        async def operation():
            async with module.renewing_async(Client(), lease):
                await asyncio.sleep(1)
        with pytest.raises(ControlError, match="LEASE_RENEWAL_FAILED"):
            await asyncio.wait_for(operation(), .3)
    asyncio.run(scenario())
