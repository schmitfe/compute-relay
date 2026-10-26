"""Experimental bounded stdio helper pool over an already authenticated master.

Every failed exchange is returned to its caller once, never replayed here.
The worker's existing journal/lease rules decide submission reconciliation.
"""

import asyncio
from copy import deepcopy
import json
import os
import signal
import struct
from uuid import uuid4

from compute_relay.backends.preparation import helper_source
from compute_relay.remote.bundles import bundle_source
from compute_relay.remote.protocol import JOB_OPERATIONS
from compute_relay.worker.client import strict_json

from .async_openssh import AsyncOpenSSHTransport, _complete_despite_cancellation, _stop
from .openssh import OpenSSHTransport, TransportError

PROTOCOL = "compute-relay-stream/v1"


class _Channel:
    def __init__(self, connection, source):
        self.transport = OpenSSHTransport(connection)
        self.source = source
        self.process = self.spawn = self.stderr = self.closing = None
        self.max_rss_kib = 0

    def _stderr_done(self, task):
        if not task.cancelled() and task.exception() is not None and self.process and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def _start(self):
        argv, _ = self.transport._helper_command(self.source, {})
        self.spawn = asyncio.create_task(asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=65536, start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"}))
        self.process = await asyncio.shield(self.spawn)
        async def drain():
            size = 0
            while chunk := await self.process.stderr.read(65536):
                size += len(chunk)
                if size > self.transport.STDERR_LIMIT:
                    raise TransportError("Persistent helper exceeded diagnostic bound")
        self.stderr = asyncio.create_task(drain())
        self.stderr.add_done_callback(self._stderr_done)

    async def exchange(self, request):
        if self.process is None:
            await self._start()
        identifier = uuid4().hex
        payload = json.dumps({"protocol": PROTOCOL, "id": identifier, "request": request},
            ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        if len(payload) > self.transport.PAYLOAD_LIMIT:
            raise TransportError("Persistent helper request exceeds frame bound")
        self.process.stdin.write(struct.pack("!I", len(payload)))
        view = memoryview(payload)
        for offset in range(0, len(view), 65536):
            self.process.stdin.write(view[offset:offset + 65536])
            await self.process.stdin.drain()
        header = await self.process.stdout.readexactly(4)
        size = struct.unpack("!I", header)[0]
        if not 1 <= size <= self.transport.STDOUT_LIMIT:
            raise TransportError("Persistent helper response exceeds frame bound")
        envelope = strict_json(await self.process.stdout.readexactly(size))
        if (not isinstance(envelope, dict) or envelope.get("protocol") != PROTOCOL
                or envelope.get("id") != identifier or envelope.get("ok") is not True
                or not isinstance(envelope.get("result"), dict)):
            raise TransportError("Persistent helper response failed protocol checks")
        if self.stderr.done() and not self.stderr.cancelled() and self.stderr.exception() is not None:
            raise TransportError("Persistent helper diagnostics failed")
        if type(envelope.get("max_rss_kib")) is int and envelope["max_rss_kib"] >= 0:
            self.max_rss_kib = max(self.max_rss_kib, envelope["max_rss_kib"])
        return envelope["result"]

    async def close(self):
        async def finish():
            if self.spawn is not None and self.process is None:
                try:
                    self.process = await asyncio.shield(self.spawn)
                except OSError:
                    return
            if self.process is not None:
                await _stop(self.process, [self.stderr] if self.stderr else [])
        if self.closing is None:
            self.closing = asyncio.create_task(finish())
        await _complete_despite_cancellation(self.closing, propagate=True)


class PersistentHelperPool:
    def __init__(self, capacity):
        if type(capacity) is not int or not 1 <= capacity <= 8:
            raise ValueError("Persistent helper capacity must be from 1 through 8")
        self.capacity = capacity
        self.source = bundle_source("stream_jobs")
        self.expected_sources = {"jobs": helper_source(), "batch": helper_source("observe_many")}
        self.slots, self.lock = asyncio.Semaphore(capacity), asyncio.Lock()
        self.channels, self.idle, self.busy = set(), [], set()
        self.configuration, self.pending, self.closed = None, 0, False
        self.process_starts, self.requests = 0, 0

    async def call_helper(self, connection, source, request):
        operation = request.get("operation")
        if operation not in JOB_OPERATIONS | {"observe_many"}:
            raise TransportError("Unsupported persistent helper operation")
        expected = self.expected_sources["batch" if operation == "observe_many" else "jobs"]
        protected = {key: connection[key] for key in ("remote_root", "slurm_paths", "scheduler_environment") if key in connection}
        if source != expected or request.get("config") != protected:
            raise TransportError("Persistent helper source or configuration is not the protected version")
        if connection.get("ssh", {}).get("mode") != "mfa" or not connection["ssh"].get("control_path"):
            raise TransportError("Persistent helpers require an authenticated SSH master")
        if self.closed or self.pending >= self.capacity * 2:
            raise TransportError("Persistent helper pool is closed or full")
        self.pending += 1
        channel = None
        try:
            async with asyncio.timeout(connection.get("command_timeout_seconds", 120)):
                async with self.slots:
                    async with self.lock:
                        if self.closed:
                            raise TransportError("Persistent helper pool is closed")
                        if self.configuration is not None and self.configuration != connection:
                            if self.busy:
                                raise TransportError("Persistent helper configuration changed while busy")
                            await asyncio.gather(*(item.close() for item in self.channels))
                            self.channels.clear(); self.idle.clear()
                        self.configuration = deepcopy(connection)
                        while self.idle and self.idle[-1].process is not None and self.idle[-1].process.returncode is not None:
                            dead = self.idle.pop()
                            self.channels.discard(dead)
                            await dead.close()
                        if self.idle:
                            channel = self.idle.pop()
                        else:
                            channel = _Channel(connection, self.source)
                            self.channels.add(channel)
                            self.process_starts += 1
                        self.busy.add(channel)
                    self.requests += 1
                    result = await channel.exchange(request)
                    async with self.lock:
                        if self.closed:
                            raise TransportError("Persistent helper pool closed during exchange")
                        self.busy.remove(channel)
                        self.idle.append(channel)
                        channel = None
                    return result
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError):
            raise TransportError("Persistent helper exchange interrupted; reconcile any submission") from None
        finally:
            try:
                if channel is not None:
                    self.busy.discard(channel)
                    self.channels.discard(channel)
                    await channel.close()
            finally:
                self.pending -= 1

    async def close(self):
        self.closed = True
        async def finish():
            await asyncio.gather(*(item.close() for item in tuple(self.channels)))
            self.channels.clear(); self.idle.clear()
        await _complete_despite_cancellation(asyncio.create_task(finish()), propagate=True)


class PersistentOpenSSHTransport(AsyncOpenSSHTransport):
    def __init__(self, connection, pool, *, retry_gate=None):
        super().__init__(connection, retry_gate=retry_gate)
        self.pool = pool

    async def _exchange_helper(self, source, request):
        if request.get("operation") in JOB_OPERATIONS | {"observe_many"}:
            return await self.pool.call_helper(self.connection, source, request)
        return await super()._exchange_helper(source, request)
