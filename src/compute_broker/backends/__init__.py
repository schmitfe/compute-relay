"""Pure package preparation and interchangeable execution backends."""

from .preparation import (
    BackendError,
    canonical_json_bytes,
    connection_revision_sha256,
    prepare,
    verify_package,
)
from .fake import FakeBackend
from .slurm import SlurmBackend
from .discovery import discover
from .environments import EnvironmentProfileError, validate_environment_config

__all__ = [
    "BackendError", "FakeBackend", "SlurmBackend", "canonical_json_bytes",
    "connection_revision_sha256", "discover", "prepare", "verify_package",
    "EnvironmentProfileError", "validate_environment_config",
]
