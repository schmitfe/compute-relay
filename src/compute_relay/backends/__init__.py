"""Pure package preparation and interchangeable execution backends."""

from .preparation import (
    BackendError,
    canonical_json_bytes,
    connection_revision_sha256,
    management_protocol_supported,
    submission_helper_is_current,
    prepare,
    verify_package,
)
from .fake import FakeBackend
from .slurm import SlurmBackend
from .async_slurm import AsyncSlurmBackend
from .discovery import discover
from .environments import EnvironmentProfileError, validate_environment_config

__all__ = [
    "BackendError", "FakeBackend", "SlurmBackend", "AsyncSlurmBackend", "canonical_json_bytes",
    "connection_revision_sha256", "discover", "prepare", "verify_package",
    "management_protocol_supported", "submission_helper_is_current",
    "EnvironmentProfileError", "validate_environment_config",
]
