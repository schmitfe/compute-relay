#!/usr/bin/env python3
"""Native system-OpenSSH acceptance using disposable loopback keys and sshd."""

import asyncio
import getpass
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from compute_relay.transports.openssh import OpenSSHTransport, TransportError
from compute_relay.worker.ssh_session import SSHSession


async def check(session):
    transport = OpenSSHTransport(session.runtime_configuration())
    source = "import json; print(json.dumps({'marker':'native-system-openssh'}))"
    assert await transport.call_helper(source, {}) == {"marker": "native-system-openssh"}
    session.close()
    try:
        await transport.call_helper(source, {})
    except TransportError:
        pass
    else:
        raise AssertionError("A dead master silently opened a fresh login")
    await session.reconnect_async()
    assert session.check()
    assert await transport.call_helper(source, {}) == {"marker": "native-system-openssh"}


def main():
    # Keep authorized_keys beneath the account's home for sshd StrictModes.
    with tempfile.TemporaryDirectory(prefix=".cr-ssh-", dir=Path.home()) as directory:
        root = Path(directory).resolve()
        for name in ("host", "client"):
            subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / name)], check=True, timeout=10)
        (root / "authorized_keys").write_bytes((root / "client.pub").read_bytes())
        (root / "authorized_keys").chmod(0o600)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        (root / "known_hosts").write_text(f"[127.0.0.1]:{port} " + (root / "host.pub").read_text())
        (root / "known_hosts").chmod(0o600)
        config = root / "sshd_config"
        config.write_text(f"""ListenAddress 127.0.0.1
Port {port}
HostKey {root}/host
PidFile {root}/sshd.pid
AuthorizedKeysFile {root}/authorized_keys
AllowUsers {getpass.getuser()}
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
StrictModes yes
PermitRootLogin no
AllowTcpForwarding no
X11Forwarding no
LogLevel ERROR
""")
        session = SSHSession({"backend": "slurm", "python_path": sys.executable, "ssh": {
            "host": "127.0.0.1", "user": getpass.getuser(), "port": port,
            "identity_file": str(root / "client"), "known_hosts": str(root / "known_hosts")}}, root / "session", lifetime_seconds=60)
        with (root / "sshd.log").open("w+") as log:
            server = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", str(config)], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while True:
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Disposable sshd failed: " + (root / "sshd.log").read_text()[-2000:])
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.1)
                session.start(interactive=False)
                asyncio.run(check(session))
            finally:
                session.close()
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
        print(json.dumps({"ok": True, "system_ssh": True, "dead_master_closed": True, "key_renewal": True}))


if __name__ == "__main__":
    main()
