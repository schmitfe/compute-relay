"""Fixed-configuration OpenSSH transport; no caller-supplied command endpoint."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal


class TransportError(RuntimeError):
    pass


class OpenSSHTransport:
    PAYLOAD_LIMIT = 150 * 1024 * 1024
    HELPER_LIMIT = 1024 * 1024
    STDOUT_LIMIT = 20 * 1024 * 1024
    STDERR_LIMIT = 65536

    def __init__(self, connection: dict, *, retry_gate=None):
        self.retry_gate = retry_gate
        self.connection = connection
        self.ssh = connection["ssh"]
        self.executable = connection.get("ssh_path", "/usr/bin/ssh")
        if not os.path.isabs(self.executable):
            raise TransportError("SSH executable must be absolute")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", self.ssh.get("host", "")) or self.ssh["host"].startswith("-"):
            raise TransportError("Invalid protected SSH host")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", self.ssh.get("user", "")):
            raise TransportError("Invalid protected SSH user")
        port = self.ssh.get("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise TransportError("Invalid SSH port")
        for field in ("identity_file", "known_hosts", "config_file", "control_path"):
            if field in self.ssh and (not isinstance(self.ssh[field], str) or not os.path.isabs(self.ssh[field]) or any(c in self.ssh[field] for c in "\x00\r\n")):
                raise TransportError("SSH paths must be protected absolute paths")
        if not self.ssh.get("known_hosts"):
            raise TransportError("Pinned known-host storage is required")
        if self.ssh.get("mode", "key") not in ("key", "mfa"):
            raise TransportError("Unsupported SSH authentication mode")
        if self.ssh.get("mode") == "mfa" and not self.ssh.get("control_path"):
            raise TransportError("MFA connections require a protected pre-established control socket")
        timeout = connection.get("command_timeout_seconds", 120)
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise TransportError("Remote command timeout must be between one and 600 seconds")

    def argv(self) -> list[str]:
        s = self.ssh
        argv = [self.executable, "-F", s.get("config_file", "/dev/null"), "-T", "-p", str(s.get("port", 22))]
        options = ["BatchMode=yes", "NumberOfPasswordPrompts=0", "StrictHostKeyChecking=yes",
                   "UserKnownHostsFile=" + s["known_hosts"], "GlobalKnownHostsFile=/dev/null",
                   "ForwardAgent=no", "ForwardX11=no", "ClearAllForwardings=yes", "PermitLocalCommand=no",
                   "ConnectTimeout=10", "ServerAliveInterval=15", "ServerAliveCountMax=2", "ControlMaster=no"]
        if s.get("mode") == "mfa":
            # A dead master must never trigger a fresh key/MFA authentication. The
            # failing fallback remains active during the check/use race.
            options += ["ControlPath=" + s["control_path"], "ProxyJump=none", "ProxyCommand=/bin/false"]
        else:
            options += ["ControlPath=none", "IdentitiesOnly=yes", "IdentityAgent=none"]
            if s.get("identity_file"):
                argv += ["-i", s["identity_file"]]
            else:
                # Absence of an explicit credential must not enable default
                # identity discovery from the service account's home.
                options += ["IdentityFile=none"]
        for option in options:
            argv += ["-o", option]
        return [*argv, "--", s["user"] + "@" + s["host"]]

    def _helper_command(self, source: str, request: dict | None) -> tuple[list[str], bytes]:
        python = self.connection.get("python_path", "/usr/bin/python3")
        if not isinstance(python, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", python):
            raise TransportError("Invalid protected remote interpreter")
        # Persistent channels send framed requests after the bootstrap bytes.
        payload = b"" if request is None else json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
        if len(payload) > self.PAYLOAD_LIMIT:
            raise TransportError("Remote operation payload exceeds limit")
        bundle = source.encode("utf-8")
        if len(bundle) > self.HELPER_LIMIT:
            raise TransportError("Remote helper exceeds limit")
        # Login shells parse only this small ASCII command. The exact byte count
        # leaves the JSON request on the same buffered stdin for the helper.
        bootstrap = f'import sys;exec(compile(sys.stdin.buffer.read({len(bundle)}),"<compute-relay-helper>","exec"))'
        command = shlex.join([python, "-I", "-c", bootstrap])
        return [*self.argv(), command], bundle + payload

    @staticmethod
    def _helper_response(stdout: bytes) -> dict:
        try:
            response = json.loads(stdout)
        except (ValueError, UnicodeError) as exc:
            raise TransportError("SSH helper returned invalid structured output") from exc
        if not isinstance(response, dict):
            raise TransportError("SSH helper response is not an object")
        return response

    async def call_helper(self, source: str, request: dict) -> dict:
        epoch = await self.retry_gate.enter() if self.retry_gate else None
        try:
            result = await self._exchange_helper(source, request)
        except BaseException:
            if self.retry_gate:
                await self.retry_gate.failed()
            raise
        if self.retry_gate:
            await self.retry_gate.succeeded(epoch)
        return result

    async def _exchange_helper(self, source, request):
        argv, payload = self._helper_command(source, request)
        return self._helper_response(await self._run(argv, payload))

    async def _run(self, argv: list[str], payload: bytes) -> bytes:
        if len(payload) > self.PAYLOAD_LIMIT + self.HELPER_LIMIT:
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
