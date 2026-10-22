"""Human-authenticated OpenSSH master; the worker only receives its socket path.

The connector owns expiry and must call ``close`` in its shutdown path. This
module never reads authentication responses or captures interactive SSH output.
"""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import stat
import subprocess

from compute_broker.transports.openssh import OpenSSHTransport, TransportError


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
        """Walk every ancestor without following symlinks, then retain its FD."""
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root_uid = os.fstat(descriptor).st_uid
        parts = self.state_directory.parts[1:]
        try:
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create or not last:
                        raise
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                metadata = os.fstat(descriptor)
                if last:
                    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                        raise TransportError("SSH session directory must be owned by this user and mode 0700")
                elif metadata.st_mode & 0o022:
                    # Root-owned sticky directories such as /tmp cannot let a
                    # different UID replace this user's private child directory.
                    if not (metadata.st_uid == root_uid and metadata.st_mode & stat.S_ISVTX):
                        raise TransportError("SSH session ancestors cannot be writable by other users")
            if not parts:
                raise TransportError("SSH session storage cannot be the filesystem root")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

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
            if self._directory_fd is not None:
                os.close(self._directory_fd)
            self._directory_fd = self._directory_identity = self._socket_identity = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
