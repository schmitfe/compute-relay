"""Generate the synthetic server trust chain accepted by strict TLS clients."""

import os
from pathlib import Path
import subprocess


def initialize(public, private, extensions):
    public, private, extensions = Path(public), Path(private), Path(extensions)
    if (public / "ca.pem").exists():
        return
    previous = os.umask(0o077)
    try:
        def run(*arguments):
            subprocess.run(["openssl", *map(str, arguments)], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=True, timeout=30)
        run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "365", "-subj", "/CN=broker-fixture-ca",
            "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout", private / "ca.key", "-out", public / "ca.pem")
        run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=ldap-fixture",
            "-keyout", private / "server.key", "-out", private / "server.csr")
        run("x509", "-req", "-days", "90", "-in", private / "server.csr", "-CA", public / "ca.pem",
            "-CAkey", private / "ca.key", "-CAserial", private / "ca.srl", "-CAcreateserial",
            "-extfile", extensions, "-out", private / "server.crt")
        run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "90", "-subj", "/CN=wrong-fixture-ca",
            "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout", private / "wrong-ca.key", "-out", public / "wrong-ca.pem")
    finally:
        os.umask(previous)


if __name__ == "__main__":
    initialize("/run/ldap-fixture", "/var/lib/ldap-fixture", "/opt/ldap-fixture/cert.extensions")
