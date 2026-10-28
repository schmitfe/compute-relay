"""Trusted factory registration changes selection, never execution authority."""

from dataclasses import FrozenInstanceError
import inspect

import pytest

from compute_relay.backends import BackendError, FakeBackend, SlurmBackend, create_backend
from compute_relay.backends.registry import BackendDefinition, BackendRegistry
from compute_relay.worker.runner import Worker


def test_explicit_factories_are_selected_without_instantiating_other_backends():
    calls = []
    def factory(name):
        def create(configuration):
            calls.append((name, configuration))
            return name
        return create
    registry = BackendRegistry((
        BackendDefinition("first", factory("first")),
        BackendDefinition("second", factory("second")),
    ))
    configuration = {"backend": "first"}
    assert registry.create(configuration) == "first"
    assert [call[0] for call in calls] == ["first"]
    assert all(call[1] is configuration for call in calls)
    with pytest.raises(TypeError):
        registry._definitions["injected"] = BackendDefinition("injected", factory("unexpected"))


@pytest.mark.parametrize("name", ["", "../module", "package.Backend", None])
def test_invalid_trusted_registrations_are_rejected(name):
    with pytest.raises(ValueError, match="Invalid trusted backend"):
        BackendRegistry([BackendDefinition(name, dict)])


def test_duplicate_and_invalid_factories_cannot_silently_replace_a_backend():
    definition = BackendDefinition("example", dict)
    with pytest.raises(ValueError, match="Duplicate backend"):
        BackendRegistry([definition, definition])
    for invalid in (BackendDefinition("example", None), BackendDefinition("example", "module.path")):
        with pytest.raises(ValueError, match="Invalid trusted backend"):
            BackendRegistry([invalid])
    with pytest.raises(FrozenInstanceError):
        definition.factory = list


@pytest.mark.parametrize("name", ["unknown", "os.system", "../module", None, [], {}])
def test_configuration_cannot_import_or_register_a_backend(name):
    with pytest.raises(BackendError, match="Unsupported configured backend"):
        create_backend({"backend": name})


def test_registered_backends_share_one_async_execution_interface(tmp_path):
    for configuration in ({"backend": "fake", "state_directory": str(tmp_path / "simulation")},
                          {"ssh": {"host": "not-contacted", "user": "broker", "known_hosts": "/protected/known_hosts"}}):
        backend = create_backend(configuration)
        for operation in ("submit", "reconcile", "observe", "cancel", "collect", "discover"):
            assert inspect.iscoroutinefunction(getattr(backend, operation))


def test_worker_uses_the_registered_builtin_factories(tmp_path):
    fake = Worker._backend({"backend": "fake", "state_directory": str(tmp_path / "simulation")})
    assert isinstance(fake, FakeBackend)
    ssh = {"host": "not-contacted", "user": "broker", "known_hosts": "/protected/known_hosts"}
    for configuration in ({"ssh": ssh}, {"backend": "slurm", "ssh": ssh}):
        assert type(Worker._backend(configuration)) is SlurmBackend
