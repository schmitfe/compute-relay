"""Shared bounded data-plane protocol; file contents never become job metadata."""

from dataclasses import dataclass

CHUNK_BYTES = 1024 * 1024
MAX_FILE_BYTES = 8 * 1024**3
MAX_BUNDLE_BYTES = 64 * 1024**3
INLINE_BYTES = 512 * 1024


@dataclass(frozen=True)
class FileReference:
    sha256: str
    size_bytes: int


def fingerprint(stream):
    import hashlib
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size
