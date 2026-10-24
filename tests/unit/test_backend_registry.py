"""Trusted factory registration changes selection, never execution authority."""

from dataclasses import FrozenInstanceError

import pytest

from compute_relay.backends import AsyncSlurmBackend, BackendError, FakeBackend, SlurmBackend, create_backend
from compute_relay.backends.registry import BackendDefinition, BackendRegistry
from compute_relay.worker.async_runner import AsyncWorker
from compute_relay.worker.runner import Worker


def test_explicit_factories_are_selected_without_instantiating_other_backends():
    calls = []
    def factory(name):
        def create(configuration):
            calls.append((name, configuration))
            return name
        return create
    registry = BackendRegistry((
        BackendDefinition("first", factory("first-sync"), factory("first-async")),
        BackendDefinition("second", factory("second-sync")),
    ))
    configuration = {"backend": "first"}
    assert registry.create(configuration) == "first-sync"
    assert registry.create(configuration, asynchronous=True) == "first-async"
    assert [call[0] for call in calls] == ["first-sync", "first-async"]
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
    for invalid in (BackendDefinition("example", None), BackendDefinition("example", dict, "module.path")):
        with pytest.raises(ValueError, match="Invalid trusted backend"):
            BackendRegistry([invalid])
    with pytest.raises(FrozenInstanceError):
        definition.sync_factory = list


@pytest.mark.parametrize("name", ["unknown", "os.system", "../module", None, [], {}])
def test_configuration_cannot_import_or_register_a_backend(name):
    with pytest.raises(BackendError, match="Unsupported configured backend"):
        create_backend({"backend": name})


def test_unsupported_async_backend_is_not_wrapped_or_silently_run_synchronously(tmp_path):
    configuration = {"backend": "fake", "state_directory": str(tmp_path / "never-created")}
    with pytest.raises(BackendError, match="no asynchronous implementation"):
        create_backend(configuration, asynchronous=True)
    assert not (tmp_path / "never-created").exists()


def test_both_workers_use_the_registered_builtin_factories(tmp_path):
    fake = Worker._backend({"backend": "fake", "state_directory": str(tmp_path / "simulation")})
    assert isinstance(fake, FakeBackend)
    ssh = {"host": "not-contacted", "user": "broker", "known_hosts": "/protected/known_hosts"}
    for configuration in ({"ssh": ssh}, {"backend": "slurm", "ssh": ssh}):
        assert type(Worker._backend(configuration)) is SlurmBackend
        assert type(AsyncWorker._backend(configuration)) is AsyncSlurmBackend
