"""Identity of this loaded local adapter, independent of the broker service."""

from hashlib import sha256
from pathlib import Path

from compute_relay import __version__


def _source_fingerprint():
    """Fingerprint core adapter sources once, before any MCP session starts.

    A long-lived process keeps its original stamp after an installation changes.
    This is an implementation diagnostic, not a signature or a claim about the
    separately deployed broker, dependencies, or optional extensions. Some
    packaged installations may not include readable Python sources.
    """
    directory = Path(__file__).parent
    digest = sha256()
    try:
        for name in ("__init__.py", "__main__.py", "facade.py", "guidance.py", "implementation.py", "requests.py", "responses.py"):
            content = (directory / name).read_bytes()
            digest.update(name.encode("ascii") + b"\0")
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except OSError:
        return None
    return digest.hexdigest()


_SOURCE_SHA256 = _source_fingerprint()


def adapter_metadata():
    """Return fresh metadata without rereading sources or consulting git."""
    return {"implementation": "compute-relay-stdio-mcp", "version": __version__,
        "schema_version": "1.0", "source_sha256": _SOURCE_SHA256,
        "transport": "stdio", "default_job_response_mode": "compact",
        "default_allowance_response_mode": "budget"}
