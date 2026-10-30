"""Bounded generation-fenced HTTP heartbeats; remote SSH stays in its adapter."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import time

from .client import ControlError
from compute_relay.diagnostics import failure

RENEWAL_INTERVAL_SECONDS = 20
RENEWAL_RETRY_SECONDS = 2
RENEWAL_SAFETY_SECONDS = 10


def _expiry(value, fallback):
    if value:
        try:
            remaining = (datetime.fromisoformat(value) - datetime.now(timezone.utc)).total_seconds()
            return time.monotonic() + max(0, remaining)
        except (ValueError, TypeError):
            pass
    return fallback


class RenewalWindow:
    def __init__(self, lease):
        now = time.monotonic()
        self.deadline = _expiry(lease.get("lease_deadline_at"), now + 40 * 60)
        self.expires = min(_expiry(lease.get("lease_expires_at"), now + 120), self.deadline)
        self.interval = RENEWAL_INTERVAL_SECONDS

    def wait(self):
        return max(0, min(self.interval, self.remaining()))

    def remaining(self):
        return self.expires - time.monotonic() - RENEWAL_SAFETY_SECONDS

    def expired(self):
        return self.remaining() <= 0

    def renewed(self, response):
        _checked_response(response)
        self.deadline = min(self.deadline, _expiry(response.get("lease_deadline_at"), self.deadline))
        self.expires = min(_expiry(response.get("lease_expires_at"), time.monotonic() + 120), self.deadline)
        self.interval = RENEWAL_INTERVAL_SECONDS

    def retry(self, exc):
        self.interval = RENEWAL_RETRY_SECONDS
        # An explicit loss of authority is final. Transport failures can recover
        # while this exact lease remains valid.
        return not (isinstance(exc, ControlError) and exc.status in {400, 401, 403, 404, 409}) and not self.expired()


def _renew_document(lease):
    return {"lease_id": lease["id"], "generation": lease["generation"]}


def _checked_response(response):
    if not isinstance(response, dict) or response.get("renewed") is not True:
        raise ControlError("LEASE_RENEWAL_FAILED")


@asynccontextmanager
async def renewing(client, lease):
    owner = asyncio.current_task()
    failed = False
    window = RenewalWindow(lease)
    cancelled_before = owner.cancelling()
    async def renew():
        nonlocal failed
        while True:
            await asyncio.sleep(window.wait())
            started = time.monotonic()
            if window.expired():
                failure(lease, "renew", ControlError("LEASE_RENEWAL_FAILED"), started)
                failed = True
                owner.cancel()
                return
            try:
                response = await asyncio.wait_for(client.request("POST", f"/internal/v1/attempts/{lease['attempt_id']}/renew",
                    _renew_document(lease)), timeout=max(.001, window.remaining()))
                window.renewed(response)
            except Exception as exc:
                if not window.retry(exc):
                    failure(lease, "renew", exc, started)
                    failed = True
                    owner.cancel()
                    return
    heartbeat = asyncio.create_task(renew())
    def check():
        if failed or window.expired():
            raise ControlError("LEASE_RENEWAL_FAILED")
    try:
        try:
            yield check
            check()
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
    except asyncio.CancelledError:
        if failed:
            owner.uncancel()
            if owner.cancelling() <= cancelled_before:
                raise ControlError("LEASE_RENEWAL_FAILED") from None
        raise
