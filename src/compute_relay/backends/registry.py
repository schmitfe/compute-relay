"""Select a fixed built-in backend without importing configuration-supplied code."""

from .fake import FakeBackend
from .preparation import BackendError
from .slurm import SlurmBackend


def create_backend(connection: dict):
    kind = connection.get("backend", "slurm")
    factory = {"fake": FakeBackend, "slurm": SlurmBackend}.get(kind) if isinstance(kind, str) else None
    if factory is None:
        raise BackendError("Unsupported configured backend")
    return factory(connection)
