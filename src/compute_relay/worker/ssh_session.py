"""Human-authenticated OpenSSH master; the worker only receives its socket path.

The connector owns expiry and must call ``close`` in its shutdown path. This
module never reads authentication responses or captures interactive SSH output.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import errno
import os
from pathlib import Path
import stat
import socket
import subprocess

from compute_relay.transports.openssh import OpenSSHTransport, TransportError


class SessionStorageError(TransportError):
    """Only fixed local diagnostics are safe to show at the CLI boundary."""
    MESSAGES = {
        "STATE_DIRECTORY_NOT_PRIVATE": "The state directory must be owned by you with permissions 0700.",
        "STATE_ANCESTOR_WRITABLE": "A parent of the state directory allows group or other-user writes. Remove those write permissions before retrying; existing directories are not changed automatically.",
        "STATE_DIRECTORY_UNSAFE": "The state path must contain accessible real directories without symlinks.",
        "STATE_PATH_INVALID": "Use a normalized absolute state path without whitespace or %; the path plus /s must fit within 100 bytes.",
    }

    def __init__(self, code):
        self.code = code
        super().__init__(self.MESSAGES[code])


def open_session_directory(directory, *, create=False):
    """Validate ancestors without following links; create each missing level privately.

    mkdir(parents=True) uses the process umask for intermediate levels. In a
    group-writable development checkout that can produce 0775 parents, which
    must never hold an authenticated SSH socket. Existing permissions stay put.
    """
    value = os.fspath(directory)
    if (not isinstance(value, str) or not os.path.isabs(value) or os.path.normpath(value) != value
            or value == "/" or any(character.isspace() or character in "%\x00" for character in value)
            or len(os.fsencode(str(Path(value) / "s"))) > 100):
        raise SessionStorageError("STATE_PATH_INVALID")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_uid = os.fstat(descriptor).st_uid
    parts = Path(value).parts[1:]
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass  # Validate a concurrent creator's directory too.
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            if last:
                if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise SessionStorageError("STATE_DIRECTORY_NOT_PRIVATE")
            else:
                if metadata.st_mode & 0o022 and not (metadata.st_uid == root_uid and metadata.st_mode & stat.S_ISVTX):
                    raise SessionStorageError("STATE_ANCESTOR_WRITABLE")
        return descriptor
    except BaseException as error:
        os.close(descriptor)
        if isinstance(error, OSError):
            raise SessionStorageError("STATE_DIRECTORY_UNSAFE") from None
        raise


class SSHSession:
    def __init__(self, connection, state_directory, lifetime_seconds=28800):
        if type(lifetime_seconds) is not int or not 60 <= lifetime_seconds <= 86400:
            raise TransportError("SSH session lifetime must be between 60 and 86400 seconds")
        if not isinstance(connection, dict) or not isinstance(connection.get("ssh"), dict):
            raise TransportError("A protected SSH connection is required")
        self.connection = deepcopy(connection)
        protected = self.connection["ssh"]
        if protected.get("mode", "key") not in ("key", "mfa"):
            raise TransportError("Unsupported SSH authentication mode")
        if not protected.get("identity_file"):
            raise TransportError("An explicit SSH identity file is required")
        for field in ("identity_file", "known_hosts"):
            value = protected.get(field)
            if not isinstance(value, str) or any(character.isspace() or character == "%" for character in value):
                raise TransportError("SSH credential paths cannot contain whitespace or expansion tokens")
        # Reuse the transport's protected host/user/port/path validation. A
        # session establishes the master, so the input need not have one yet.
        validated = deepcopy(self.connection)
        validated["ssh"] = {**protected, "mode": "key", "config_file": "/dev/null"}
        validated["ssh"].pop("control_path", None)
        validated["ssh_path"] = "/usr/bin/ssh"
        OpenSSHTransport(validated)
        directory = os.fspath(state_directory)
        if (not isinstance(directory, str) or not os.path.isabs(directory) or os.path.normpath(directory) != directory
                or any(character.isspace() or character in "%\x00" for character in directory)):
            raise TransportError("SSH session storage must be a normalized absolute path")
        self.state_directory = Path(directory)
        self.socket_path = str(self.state_directory / "s")
        if len(os.fsencode(self.socket_path)) > 100:
            raise TransportError("SSH control socket path exceeds 100 bytes")
        self.lifetime_seconds = lifetime_seconds
        self._directory_fd = None
        self._directory_identity = None
        self._socket_identity = None

    def runtime_configuration(self):
        """Stable, private worker configuration; it cannot initiate a login."""
        result = deepcopy(self.connection)
        result["ssh_path"] = "/usr/bin/ssh"
        result["ssh"].update(mode="mfa", control_path=self.socket_path, config_file="/dev/null")
        result["ssh"].pop("identity_file", None)
        return result

    @staticmethod
    def _identity(metadata):
        return metadata.st_dev, metadata.st_ino

    def _open_directory(self, *, create=False):
        return open_session_directory(self.state_directory, create=create)

    def _directory_matches(self):
        if self._directory_fd is None:
            return False
        fresh = None
        try:
            fresh = self._open_directory()
            return self._identity(os.fstat(fresh)) == self._directory_identity == self._identity(os.fstat(self._directory_fd))
        except (OSError, TransportError):
            return False
        finally:
            if fresh is not None:
                os.close(fresh)

    def _socket_metadata(self):
        if not self._directory_matches():
            return None
        try:
            metadata = os.stat("s", dir_fd=self._directory_fd, follow_symlinks=False)
        except OSError:
            return None
        if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077 or metadata.st_nlink != 1):
            return None
        return metadata

    def _base_argv(self, *, interactive=False):
        ssh = self.connection["ssh"]
        options = ["BatchMode=" + ("no" if interactive else "yes"),
            "NumberOfPasswordPrompts=" + ("3" if interactive else "0"),
            "StrictHostKeyChecking=" + ("ask" if interactive else "yes"),
            "UserKnownHostsFile=" + ssh["known_hosts"], "GlobalKnownHostsFile=/dev/null",
            "ForwardAgent=no", "ForwardX11=no", "ForwardX11Trusted=no", "ClearAllForwardings=yes",
            "PermitLocalCommand=no", "IdentityAgent=none", "IdentitiesOnly=yes", "ProxyJump=none", "UpdateHostKeys=no",
            "ConnectTimeout=10", "ServerAliveInterval=15", "ServerAliveCountMax=2", "ControlPersist=no"]
        argv = ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-p", str(ssh.get("port", 22))]
        for option in options:
            argv += ["-o", option]
        return argv

    def _target(self):
        return self.connection["ssh"]["user"] + "@" + self.connection["ssh"]["host"]

    def _control_argv(self, operation):
        argv = self._base_argv()
        for option in ("ControlMaster=no", "ProxyCommand=/bin/false", "IdentityFile=none"):
            argv += ["-o", option]
        return [*argv, "-S", self.socket_path, "-O", operation, "--", self._target()]

    @staticmethod
    def _run(argv, *, interactive=False, timeout=10):
        # No SSH_AUTH_SOCK, SSH_ASKPASS, DISPLAY, shell startup, or ambient config.
        # OpenSSH owns human prompts on the inherited controlling terminal.
        return subprocess.run(argv, stdin=None if interactive else subprocess.DEVNULL,
            stdout=None if interactive else subprocess.DEVNULL,
            stderr=None if interactive else subprocess.DEVNULL, check=False, timeout=timeout,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"})

    def start(self, interactive=True):
        if type(interactive) is not bool:
            raise TransportError("Interactive SSH startup must be explicitly enabled or disabled")
        if self._socket_identity is not None:
            if self.check():
                return self
            self.close()
        launched = False
        try:
            self._directory_fd = self._open_directory(create=True)
            self._directory_identity = self._identity(os.fstat(self._directory_fd))
            try:
                os.stat("s", dir_fd=self._directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise TransportError("Refusing an existing unknown SSH control socket or file")
            argv = self._base_argv(interactive=interactive)
            argv += ["-o", "ProxyCommand=none", "-i", self.connection["ssh"]["identity_file"],
                "-M", "-N", "-f", "-S", self.socket_path, "--", self._target()]
            launched = True
            result = self._run(argv, interactive=interactive, timeout=min(self.lifetime_seconds, 300))
            metadata = self._socket_metadata()
            if metadata is not None:
                self._socket_identity = self._identity(metadata)
            if result.returncode != 0 or self._socket_identity is None or not self.check():
                raise TransportError("SSH login did not establish a usable private master")
            return self
        except BaseException as error:
            # A timed-out or interrupted ssh -f may already have established a
            # master. Claim only the new private socket in our unchanged directory.
            if launched and self._socket_identity is None:
                metadata = self._socket_metadata()
                if metadata is not None:
                    self._socket_identity = self._identity(metadata)
            self.close()
            if isinstance(error, (OSError, subprocess.TimeoutExpired)):
                raise TransportError("SSH login failed or timed out") from None
            raise

    def check(self):
        metadata = self._socket_metadata()
        if metadata is None or self._identity(metadata) != self._socket_identity:
            return False
        try:
            return self._run(self._control_argv("check")).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    async def _control_async(self, operation):
        from compute_relay.transports.openssh import OpenSSHTransport
        configuration = self.runtime_configuration()
        configuration["command_timeout_seconds"] = 10
        transport = OpenSSHTransport(configuration)
        transport.STDOUT_LIMIT = transport.STDERR_LIMIT = 16384
        await transport._run(self._control_argv(operation), b"")

    async def check_async(self):
        """Check only our validated master without blocking other connectors."""
        metadata = self._socket_metadata()
        if metadata is None or self._identity(metadata) != self._socket_identity:
            return False
        try:
            await self._control_async("check")
            return True
        except TransportError:
            return False

    async def retire_async(self):
        """Retire our master; remove only its proven refused, unchanged socket."""
        try:
            metadata = self._socket_metadata()
            if metadata is not None and self._identity(metadata) == self._socket_identity:
                try:
                    await self._control_async("exit")
                except TransportError:
                    pass
            if not self._directory_matches():
                return False
            try:
                metadata = os.stat("s", dir_fd=self._directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return True
            if self._identity(metadata) != self._socket_identity or self._socket_metadata() is None:
                return False
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.setblocking(False)
                refused = probe.connect_ex(self.socket_path) == errno.ECONNREFUSED
            if not refused or not self._directory_matches():
                return False
            metadata = os.stat("s", dir_fd=self._directory_fd, follow_symlinks=False)
            if self._identity(metadata) != self._socket_identity:
                return False
            os.unlink("s", dir_fd=self._directory_fd)
            return True
        finally:
            self._release_directory()

    async def reconnect_async(self):
        """One bounded public-key-only attempt; cannot prompt or replay MFA."""
        from compute_relay.transports.openssh import OpenSSHTransport
        if self._directory_fd is not None:
            raise TransportError("Retire the previous SSH session before reconnecting")
        launched = False
        try:
            self._directory_fd = self._open_directory()
            self._directory_identity = self._identity(os.fstat(self._directory_fd))
            try:
                os.stat("s", dir_fd=self._directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise TransportError("Refusing an existing SSH socket during recovery")
            argv = self._base_argv()
            for option in ("PreferredAuthentications=publickey", "PasswordAuthentication=no",
                    "KbdInteractiveAuthentication=no", "ConnectionAttempts=1", "ProxyCommand=none"):
                argv += ["-o", option]
            argv += ["-i", self.connection["ssh"]["identity_file"], "-M", "-N", "-f", "-S", self.socket_path, "--", self._target()]
            configuration = self.runtime_configuration()
            configuration["command_timeout_seconds"] = 30
            launched = True
            await OpenSSHTransport(configuration)._run(argv, b"")
            metadata = self._socket_metadata()
            if metadata is not None:
                self._socket_identity = self._identity(metadata)
            if self._socket_identity is None or not await self.check_async():
                raise TransportError("SSH recovery did not establish a usable private master")
        except BaseException as error:
            if launched and self._socket_identity is None:
                metadata = self._socket_metadata()
                if metadata is not None:
                    self._socket_identity = self._identity(metadata)
            await self.close_async()
            if isinstance(error, (OSError, TimeoutError)):
                raise TransportError("SSH recovery failed or timed out") from None
            raise

    def _release_directory(self):
        if self._directory_fd is not None:
            os.close(self._directory_fd)
        self._directory_fd = self._directory_identity = self._socket_identity = None

    async def close_async(self):
        """Finish owned master shutdown even if application shutdown is cancelled."""
        from compute_relay.transports.openssh import _complete_despite_cancellation
        try:
            metadata = self._socket_metadata()
            if metadata is not None and self._identity(metadata) == self._socket_identity:
                try:
                    await _complete_despite_cancellation(asyncio.create_task(self._control_async("exit")), propagate=True)
                except TransportError:
                    pass
        finally:
            self._release_directory()

    def close(self):
        """Ask only our validated master to exit; never connect or unlink a file."""
        try:
            metadata = self._socket_metadata()
            if metadata is not None and self._identity(metadata) == self._socket_identity:
                try:
                    self._run(self._control_argv("exit"))
                except (OSError, subprocess.TimeoutExpired):
                    pass
        finally:
            self._release_directory()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
