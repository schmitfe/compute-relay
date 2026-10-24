"""Bounded generation-fenced HTTP heartbeats; remote SSH stays in its adapter."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
import threading

from .client import ControlError

RENEWAL_INTERVAL_SECONDS = 20


def _renew_document(lease):
    return {"lease_id": lease["id"], "generation": lease["generation"]}


def _checked_response(response):
    if not isinstance(response, dict) or response.get("renewed") is not True:
        raise ControlError("LEASE_RENEWAL_FAILED")


@contextmanager
def renewing(client, lease):
    """The sync adapter's background thread performs HTTP only, never SSH."""
    stopped, failed = threading.Event(), []
    def renew():
        while not stopped.wait(RENEWAL_INTERVAL_SECONDS):
            try:
                _checked_response(client.request("POST", f"/internal/v1/attempts/{lease['attempt_id']}/renew", _renew_document(lease)))
            except Exception:
                failed.append(True)
                return
    thread = threading.Thread(target=renew, name="compute-relay-lease-renewal", daemon=True)
    thread.start()
    def check():
        if failed:
            raise ControlError("LEASE_RENEWAL_FAILED")
    try:
        yield check
        check()
    finally:
        stopped.set()
        thread.join(timeout=1)


@asynccontextmanager
async def renewing_async(client, lease):
    owner = asyncio.current_task()
    failed = False
    async def renew():
        nonlocal failed
        while True:
            await asyncio.sleep(RENEWAL_INTERVAL_SECONDS)
            try:
                _checked_response(await client.request("POST", f"/internal/v1/attempts/{lease['attempt_id']}/renew", _renew_document(lease)))
            except Exception:
                failed = True
                owner.cancel()
                return
    heartbeat = asyncio.create_task(renew())
    try:
        try:
            yield lambda: None
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
    except asyncio.CancelledError:
        if failed:
            raise ControlError("LEASE_RENEWAL_FAILED") from None
        raise
