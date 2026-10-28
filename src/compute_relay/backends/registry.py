"""Explicit registration of trusted, installed backend implementations.

Configuration selects an identifier in this immutable registry. It never names
an import path, discovers entry points, or adds code to a running worker.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import re
from types import MappingProxyType

from .fake import FakeBackend
from .preparation import BackendError
from .slurm import SlurmBackend


@dataclass(frozen=True)
class BackendDefinition:
    name: str
    factory: Callable[[dict], object]


class BackendRegistry:
    def __init__(self, definitions: Iterable[BackendDefinition]):
        registered = {}
        for definition in definitions:
            if (not isinstance(definition.name, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", definition.name)
                    or not callable(definition.factory)):
                raise ValueError("Invalid trusted backend definition")
            if definition.name in registered:
                raise ValueError("Duplicate backend registration")
            registered[definition.name] = definition
        self._definitions = MappingProxyType(registered)

    def create(self, connection: dict):
        kind = connection.get("backend", "slurm")
        definition = self._definitions.get(kind) if isinstance(kind, str) else None
        if definition is None:
            raise BackendError("Unsupported configured backend")
        return definition.factory(connection)


# Keep the installed-code allowlist reviewable in one place. Adding a factory
# does not supply a new backend's policy, schemas, preparation or identity rules.
_BACKENDS = BackendRegistry((
    BackendDefinition("fake", FakeBackend),
    BackendDefinition("slurm", SlurmBackend),
))


def create_backend(connection: dict):
    return _BACKENDS.create(connection)
