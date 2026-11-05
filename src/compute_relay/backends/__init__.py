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
from .environments import EnvironmentProfileError, validate_environment_config
from .registry import create_backend

__all__ = [
    "BackendError", "FakeBackend", "SlurmBackend", "canonical_json_bytes",
    "connection_revision_sha256", "prepare", "verify_package",
    "management_protocol_supported", "submission_helper_is_current",
    "EnvironmentProfileError", "validate_environment_config",
    "create_backend",
]
