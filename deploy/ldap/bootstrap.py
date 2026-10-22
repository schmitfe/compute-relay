"""Generate synthetic fixture credentials privately, once per persistent volume."""

import os
from pathlib import Path
import secrets
import stat
import subprocess

ACCOUNTS = ("reader", "alice", "bob", "disabled", "expired")


def initialize(public, private, template, *, owner=10001):
    public, private, template = Path(public), Path(private), Path(template)
    public.mkdir(mode=0o755, parents=True, exist_ok=True)
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing_database = (private / "data" / "data.mdb").exists()
    for account in ACCOUNTS:
        path = public / (account + "-password")
        if existing_database and not path.exists():
            raise RuntimeError("Persistent fixture credential volume is missing")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_nlink != 1:
                raise RuntimeError("Unsafe fixture credential file")
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(32))
            os.chown(path, owner, owner)
    if existing_database:
        return
    seed = template.read_text()
    for account in ACCOUNTS:
        # -T reads a private file: no password is passed in process arguments.
        result = subprocess.run(["slappasswd", "-T", str(public / (account + "-password"))],
            capture_output=True, text=True, check=True, timeout=5)
        encoded = result.stdout.strip()
        if not encoded.startswith("{SSHA}") or "\n" in encoded:
            raise RuntimeError("Unexpected fixture password hash")
        seed = seed.replace("@@" + account.upper() + "_PASSWORD@@", encoded)
    path = private / "seed.generated.ldif"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(seed)


if __name__ == "__main__":
    initialize("/run/ldap-fixture", "/var/lib/ldap-fixture", "/opt/ldap-fixture/seed.ldif")
