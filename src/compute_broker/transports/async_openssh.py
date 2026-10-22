"""Async OpenSSH processes with fixed configuration and bounded pipe streaming.

There are no thread-pool wrappers or automatic retries. Cancelling a call kills
its local process group, drains/reaps the child, and propagates cancellation;
the caller must retain any already-written submission intent for reconciliation.
"""

from __future__ import annotations

import asyncio
import os
import signal

from .openssh import OpenSSHTransport, TransportError


class AsyncOpenSSHTransport(OpenSSHTransport):
    async def call_helper(self, source: str, request: dict) -> dict:
        argv, payload = self._helper_command(source, request)
        return self._helper_response(await self._run(argv, payload))

    async def _run(self, argv: list[str], payload: bytes) -> bytes:
        if len(payload) > self.PAYLOAD_LIMIT:
            raise TransportError("Remote operation payload exceeds limit")
        process = None
        tasks = []
        deadline = asyncio.get_running_loop().time() + self.connection.get("command_timeout_seconds", 120)
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=65536, start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        ))
        try:
            # Shield only creation so cancellation cannot lose a just-spawned
            # child before we have a handle with which to stop and reap it.
            process = await asyncio.shield(spawn)
            output = bytearray()

            async def feed():
                try:
                    view = memoryview(payload)
                    for offset in range(0, len(view), 65536):
                        process.stdin.write(view[offset:offset + 65536])
                        await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    # The return code and bounded output determine the result.
                    pass
                finally:
                    process.stdin.close()

            async def read_output():
                while chunk := await process.stdout.read(65536):
                    if len(output) + len(chunk) > self.STDOUT_LIMIT:
                        raise TransportError("SSH helper exceeded stdout limit")
                    output.extend(chunk)

            async def discard_diagnostics():
                size = 0
                while chunk := await process.stderr.read(65536):
                    size += len(chunk)
                    if size > self.STDERR_LIMIT:
                        raise TransportError("SSH helper exceeded diagnostic limit")

            tasks = [asyncio.create_task(operation) for operation in
                     (feed(), read_output(), discard_diagnostics(), process.wait())]
            try:
                async with asyncio.timeout_at(deadline):
                    await asyncio.gather(*tasks)
            except TimeoutError:
                raise TransportError("SSH helper timed out; reconcile any submission") from None
            if process.returncode != 0:
                raise TransportError("SSH helper did not complete successfully")
            return bytes(output)
        except asyncio.CancelledError:
            # create_subprocess_exec may complete concurrently with cancellation.
            # Its result remains owned by this call until cleanup has finished.
            if process is None:
                process = await _complete_despite_cancellation(spawn)
            raise
        except OSError:
            raise TransportError("SSH operation failed or timed out; submission may be uncertain") from None
        finally:
            if process is not None:
                await _complete_despite_cancellation(asyncio.create_task(_stop(process, tasks)), propagate=True)


async def _stop(process, tasks):
    if process.returncode is None or any(not task.done() for task in tasks):
        # The new session contains this SSH operation only. This also stops an
        # explicit protected proxy subprocess that keeps inherited pipes open.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.stdin:
        process.stdin.close()
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await process.wait()


async def _complete_despite_cancellation(task, *, propagate=False):
    """Complete owned process cleanup even if shutdown requests arrive twice."""
    interrupted = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            interrupted = True
            if task.done():
                result = task.result()
                break
    if interrupted and propagate:
        raise asyncio.CancelledError
    return result
