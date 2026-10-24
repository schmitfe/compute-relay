"""Explicit registration of trusted, installed backend implementations.

Configuration selects an identifier in this immutable registry. It never names
an import path, discovers entry points, or adds code to a running worker.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import re
from types import MappingProxyType

from .async_slurm import AsyncSlurmBackend
from .fake import FakeBackend
from .preparation import BackendError
from .slurm import SlurmBackend


@dataclass(frozen=True)
class BackendDefinition:
    name: str
    sync_factory: Callable[[dict], object]
    async_factory: Callable[[dict], object] | None = None


class BackendRegistry:
    def __init__(self, definitions: Iterable[BackendDefinition]):
        registered = {}
        for definition in definitions:
            if (not isinstance(definition.name, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", definition.name)
                    or not callable(definition.sync_factory)
                    or definition.async_factory is not None and not callable(definition.async_factory)):
                raise ValueError("Invalid trusted backend definition")
            if definition.name in registered:
                raise ValueError("Duplicate backend registration")
            registered[definition.name] = definition
        self._definitions = MappingProxyType(registered)

    def create(self, connection: dict, *, asynchronous: bool = False):
        kind = connection.get("backend", "slurm")
        definition = self._definitions.get(kind) if isinstance(kind, str) else None
        if definition is None:
            raise BackendError("Unsupported configured backend")
        factory = definition.async_factory if asynchronous else definition.sync_factory
        if factory is None:
            raise BackendError("Configured backend has no asynchronous implementation")
        return factory(connection)


# Keep the installed-code allowlist reviewable in one place. Adding a factory
# does not supply a new backend's policy, schemas, preparation or identity rules.
_BACKENDS = BackendRegistry((
    BackendDefinition("fake", FakeBackend),
    BackendDefinition("slurm", SlurmBackend, AsyncSlurmBackend),
))


def create_backend(connection: dict, *, asynchronous: bool = False):
    return _BACKENDS.create(connection, asynchronous=asynchronous)
