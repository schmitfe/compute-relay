"""Fixed-configuration OpenSSH transport; no caller-supplied command endpoint."""

from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import subprocess
import tempfile
import time


class TransportError(RuntimeError):
    pass


class OpenSSHTransport:
    PAYLOAD_LIMIT = 150 * 1024 * 1024
    STDOUT_LIMIT = 20 * 1024 * 1024
    STDERR_LIMIT = 65536

    def __init__(self, connection: dict):
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

    def _helper_command(self, source: str, request: dict) -> tuple[list[str], bytes]:
        python = self.connection.get("python_path", "/usr/bin/python3")
        if not isinstance(python, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", python):
            raise TransportError("Invalid protected remote interpreter")
        payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
        if len(payload) > self.PAYLOAD_LIMIT:
            raise TransportError("Remote operation payload exceeds limit")
        command = shlex.join([python, "-I", "-c", source])
        return [*self.argv(), command], payload

    @staticmethod
    def _helper_response(stdout: bytes) -> dict:
        try:
            response = json.loads(stdout)
        except (ValueError, UnicodeError) as exc:
            raise TransportError("SSH helper returned invalid structured output") from exc
        if not isinstance(response, dict):
            raise TransportError("SSH helper response is not an object")
        return response

    def call_helper(self, source: str, request: dict) -> dict:
        argv, payload = self._helper_command(source, request)
        return self._helper_response(self._run(argv, payload))

    def _run(self, argv: list[str], payload: bytes) -> bytes:
        """Bound both pipes while streaming; neither RAM nor diagnostics grow forever."""
        process = None
        output = bytearray()
        error_size = 0
        deadline = time.monotonic() + self.connection.get("command_timeout_seconds", 120)
        try:
            # An unlinked private file avoids deadlocks while delivering a large
            # request. Agent source is stored as data, never executed locally.
            with tempfile.TemporaryFile() as incoming, selectors.DefaultSelector() as selector:
                incoming.write(payload)
                incoming.seek(0)
                process = subprocess.Popen(argv, stdin=incoming, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env={"PATH": "/usr/bin:/bin", "LANG": "C"})
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TransportError("SSH helper timed out; reconcile any submission")
                    for key, _ in selector.select(min(remaining, 0.25)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        if key.data == "stdout":
                            output.extend(chunk)
                            if len(output) > self.STDOUT_LIMIT:
                                raise TransportError("SSH helper exceeded stdout limit")
                        else:
                            error_size += len(chunk)
                            if error_size > self.STDERR_LIMIT:
                                raise TransportError("SSH helper exceeded diagnostic limit")
                remaining = deadline - time.monotonic()
                if remaining <= 0 or process.wait(timeout=remaining) != 0:
                    raise TransportError("SSH helper did not complete successfully")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransportError("SSH operation failed or timed out; submission may be uncertain") from exc
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
        return bytes(output)
