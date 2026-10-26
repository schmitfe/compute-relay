"""Bounded outage recovery, without password/MFA replay or concurrent probes."""

import asyncio
import random
import time

from compute_relay.transports.openssh import TransportError


class RetryGate:
    """Shared exponential cooldown; only one operation tests an impaired peer."""

    def __init__(self, *, maximum=60, clock=time.monotonic, jitter=None):
        self.maximum, self.clock = maximum, clock
        self.jitter = jitter or (lambda: random.uniform(1, 1.2))
        self.failures, self.ready_at, self.probing = 0, 0, False
        self.epoch = 0
        self.changed = asyncio.Condition()

    async def enter(self):
        async with self.changed:
            while self.failures:
                if self.probing:
                    await self.changed.wait()
                else:
                    delay = self.ready_at - self.clock()
                    if delay > 0:
                        try:
                            await asyncio.wait_for(self.changed.wait(), timeout=delay)
                        except TimeoutError:
                            pass
                    else:
                        self.probing = True
                        return self.epoch
            return self.epoch

    async def failed(self):
        async with self.changed:
            self.failures += 1
            self.epoch += 1
            self.ready_at = self.clock() + min(self.maximum, 2 ** min(self.failures, 6) * self.jitter())
            self.probing = False
            self.changed.notify_all()

    async def succeeded(self, epoch):
        async with self.changed:
            if epoch != self.epoch:
                return  # An older in-flight success cannot erase a newer outage.
            self.failures, self.ready_at, self.probing = 0, 0, False
            self.epoch += 1
            self.changed.notify_all()


async def reachable(host, port):
    """TCP only: no SSH username, key, password or authentication exchange."""
    writer = None
    try:
        async with asyncio.timeout(5):
            _, writer = await asyncio.open_connection(host, port)
            return True
    except (OSError, TimeoutError):
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass


class SessionRecovery:
    """At most three automatic key authentications during one connector launch.

    A single failed authentication/handshake opens the circuit until the human
    explicitly restarts. Each outage permits at most twelve network-only probes.
    """

    def __init__(self, *, probe=reachable, sleep=asyncio.sleep, clock=time.monotonic, jitter=None):
        self.probe, self.sleep, self.clock = probe, sleep, clock
        self.jitter = jitter or (lambda: random.uniform(1, 1.2))
        self.attempts, self.last_attempt, self.blocked = 0, None, False

    async def reconnect(self, session, status):
        if self.blocked or self.attempts >= 3:
            status.problem("Automatic SSH recovery limit reached. Restart this connector to sign in explicitly.")
            return False
        status.problem("SSH connection interrupted. Job state is retained; checking network availability with backoff.")
        if not await session.retire_async():
            self.blocked = True
            status.problem("The previous SSH socket could not be retired safely. Check it before restarting this connector.")
            return False
        ssh = session.connection["ssh"]
        for index in range(12):
            delay = min(60, 2 ** (index + 1) * self.jitter())
            if self.last_attempt is not None:
                delay = max(delay, 60 - (self.clock() - self.last_attempt))
            status.progress(f"Next network check in {delay:.0f} seconds; no credentials are sent by this check.")
            await self.sleep(delay)
            if not await self.probe(ssh["host"], ssh.get("port", 22)):
                continue
            self.attempts += 1
            self.last_attempt = self.clock()
            try:
                await session.reconnect_async()
            except TransportError:
                self.blocked = True
                status.problem("Automatic SSH reconnection failed. No further authentication attempts will be made. Restart the connector for passphrase/MFA login or to check credentials and host keys.")
                return False
            status.notice("SSH connection restored. Reconciling recorded work before continuing.")
            return True
        self.blocked = True
        status.problem("Network recovery probe limit reached. Restart this connector when the cluster is reachable.")
        return False
