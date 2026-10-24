"""Local supervision without real SSH, second factors or background web services."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from compute_relay import local_connectors as local
from compute_relay.connector_profiles import ConnectorProfiles
from compute_relay.worker import personal_connector
from compute_relay.worker.connector_status import ConnectorStatus


ORIGIN = "http://127.0.0.1:8768"


@pytest.fixture
def data_directory():
    # Real OpenSSH control-socket path validation also applies to fake state.
    with tempfile.TemporaryDirectory(prefix="cb-local-") as value:
        yield Path(value)


def saved(data, name, *, origin=ORIGIN, registration_id=None):
    identifier = str(registration_id or uuid4())
    directory = local.connector_directory(data, identifier)
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    personal_connector.write_private(directory / "state.json", {"broker_url": origin,
        "registration_id": identifier, "name": name})
    registry = ConnectorProfiles(data / "connectors-registry")
    registry.add(name, directory)
    return directory


def request_document(*, name="lab", origin=ORIGIN, registration_id=None, pairing=True):
    identifier = str(registration_id or uuid4())
    target = {"host": "cluster.example.invalid", "user": "scientist", "port": 22}
    setup = {"schema_version": "1.0", "registration_id": identifier, "broker_url": origin,
        "owner_id": str(uuid4()), "name": name, "target": target, "remote_root": "/scratch/jobs",
        "pairing_token": "synthetic-private-pairing-token", "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "local": {"identity_file": "/private/synthetic-key"}, "site_profile": {"automatic_execution_allowed": False}}
    return {"schema_version": "1.0", "request_id": str(uuid4()), "registration_id": identifier,
        "broker_url": origin, "name": name, "target": target, "setup": setup if pairing else None}


class OnePass:
    def __init__(self, on_wait=None):
        self.stopped = False
        self.on_wait = on_wait

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        if self.on_wait:
            self.on_wait()
        else:
            self.stopped = True


@pytest.mark.parametrize("origin", ["http://example.com", "http://127.0.0.1:0", "http://127.0.0.1/path",
    "http://user:password@127.0.0.1", "http://localhost?target=x", "http://127.0.0.1\n", "file:///tmp/foo"])
def test_origin_must_be_fixed_loopback_origin(origin):
    with pytest.raises(local.LocalConnectorError):
        local.loopback_origin(origin)


def test_private_queue_keeps_setup_out_of_status_and_serializes_duplicate_issuance(data_directory):
    requests = local.LocalConnectorRequests(data_directory)
    document = request_document()
    calls = []
    def issue():
        calls.append(True)
        return document
    assert requests.enqueue(document["registration_id"], issue) == document["request_id"]
    with pytest.raises(local.LocalConnectorError, match="already waiting"):
        requests.enqueue(document["registration_id"], issue)
    assert len(calls) == 1
    assert requests.directory.stat().st_mode & 0o777 == 0o700
    for file in requests.directory.iterdir():
        assert file.stat().st_mode & 0o777 == 0o600
    assert requests.take(ORIGIN) == document
    assert requests.take(ORIGIN) is None


@pytest.mark.parametrize("mutation", ["other-origin", "path-traversal", "host-option", "user-shell", "extra-field"])
def test_start_requests_reject_scope_and_injection(data_directory, mutation):
    requests = local.LocalConnectorRequests(data_directory)
    document = request_document()
    if mutation == "other-origin":
        document["broker_url"] = "https://broker.example.invalid"
    elif mutation == "path-traversal":
        document["registration_id"] = "../../elsewhere"
    elif mutation == "host-option":
        document["target"]["host"] = "-oProxyCommand=evil"
    elif mutation == "user-shell":
        document["target"]["user"] = "user;touch /tmp/evil"
    else:
        document["state_directory"] = "/tmp/other"
    with pytest.raises((local.LocalConnectorError, ValueError)):
        requests.enqueue(uuid4(), lambda: document)
    assert not list(requests.directory.glob("*.json"))


def test_malformed_request_is_discarded_once_without_secret_output(data_directory):
    requests = local.LocalConnectorRequests(data_directory)
    document = request_document()
    requests.enqueue(document["registration_id"], lambda: document)
    pending = next(requests.directory.glob("*.json"))
    pending.write_text("synthetic-secret-invalid-json")
    with pytest.raises(local.LocalConnectorError) as caught:
        requests.take(ORIGIN)
    assert "synthetic-secret" not in str(caught.value)
    assert requests.take(ORIGIN) is None


def test_request_symlink_does_not_read_or_remove_target(data_directory):
    requests = local.LocalConnectorRequests(data_directory)
    document = request_document()
    requests.enqueue(document["registration_id"], lambda: document)
    pending = next(requests.directory.glob("*.json"))
    pending.unlink()
    outside = data_directory / "outside"
    outside.write_text("preserve synthetic secret")
    pending.symlink_to(outside)
    with pytest.raises(local.LocalConnectorError):
        requests.take(ORIGIN)
    assert outside.read_text() == "preserve synthetic secret"
    assert not pending.is_symlink()


def test_setup_file_only_exists_while_authenticating(data_directory):
    requests = local.LocalConnectorRequests(data_directory)
    document = request_document()
    with requests.setup_file(document) as path:
        assert personal_connector.read_private(path) == document["setup"]
        assert path.stat().st_mode & 0o777 == 0o600
    assert not path.exists()


def test_saved_connectors_authenticate_sequentially_and_keep_serving_during_later_prompts(data_directory, monkeypatch):
    saved(data_directory, "agmn")
    saved(data_directory, "busy")
    saved(data_directory, "Ramses")
    events = []
    first_serving = threading.Event()
    stop = OnePass()
    main_thread = threading.get_ident()

    @contextmanager
    def connected(args):
        assert threading.get_ident() == main_thread
        label = args.connector_label
        events.append(("authenticate", label))
        if label == "busy":
            raise BlockingIOError
        if label == "Ramses":
            assert first_serving.wait(2), "First connector did not serve while the later login started"
        try:
            yield {"name": label}, object(), Path(args.state_directory), None, ConnectorStatus({"name": label})
        finally:
            events.append(("closed", label))

    async def serve(state, session, directory, **kwargs):
        events.append(("serve", state["name"]))
        if state["name"] == "agmn":
            first_serving.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(personal_connector, "connected", connected)
    monkeypatch.setattr(personal_connector, "serve", serve)
    local.run_connectors(ORIGIN, data_directory, stop_event=stop, poll_interval=0.001)
    assert [label for event, label in events if event == "authenticate"] == ["agmn", "busy", "Ramses"]
    assert {label for event, label in events if event == "closed"} == {"agmn", "Ramses"}
    assert events.index(("serve", "agmn")) < events.index(("closed", "agmn"))


def test_failed_login_does_not_retry_or_stop_other_connectors(data_directory, monkeypatch, capsys):
    saved(data_directory, "failed")
    saved(data_directory, "healthy")
    calls = []
    stop = threading.Event()

    @contextmanager
    def connected(args):
        calls.append(args.connector_label)
        if args.connector_label == "failed":
            raise ValueError("synthetic-private-token must not leak")
        try:
            yield {"name": "healthy"}, object(), Path(args.state_directory), None, ConnectorStatus({"name": "healthy"})
        finally:
            calls.append("closed")

    async def serve(*args, **kwargs):
        stop.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(personal_connector, "connected", connected)
    monkeypatch.setattr(personal_connector, "serve", serve)
    local.run_connectors(ORIGIN, data_directory, stop_event=stop, poll_interval=0.001)
    output = capsys.readouterr()
    assert calls == ["failed", "healthy", "closed"]
    assert "other clusters remain available" in output.err
    assert "synthetic-private-token" not in output.out + output.err


def test_saved_connector_for_another_broker_is_not_started(data_directory, monkeypatch, capsys):
    saved(data_directory, "foreign", origin="https://broker.example.invalid")
    monkeypatch.setattr(personal_connector, "connected", lambda _: pytest.fail("Foreign broker triggered SSH"))
    local.run_connectors(ORIGIN, data_directory, stop_event=OnePass(), poll_interval=0.001)
    assert "belongs to another broker" in capsys.readouterr().out


def test_dynamic_registry_changes_are_observed_after_initial_empty_scan(data_directory, monkeypatch):
    calls = []
    stop = OnePass(on_wait=lambda: saved(data_directory, "added") if not calls else None)

    @contextmanager
    def connected(args):
        calls.append(args.connector_label)
        try:
            yield {"name": "added"}, object(), Path(args.state_directory), None, ConnectorStatus({"name": "added"})
        finally:
            calls.append("closed")

    async def serve(*args, **kwargs):
        stop.stopped = True
        await asyncio.Event().wait()

    monkeypatch.setattr(personal_connector, "connected", connected)
    monkeypatch.setattr(personal_connector, "serve", serve)
    deadline = time.monotonic() + 2
    local.run_connectors(ORIGIN, data_directory, stop_event=stop, server_alive=lambda: time.monotonic() < deadline, poll_interval=0.001)
    assert calls == ["added", "closed"]


def test_dynamic_pairing_uses_private_setup_and_remembers_success(data_directory, monkeypatch, capsys):
    document = request_document(name="New lab cluster")
    requests = local.LocalConnectorRequests(data_directory)
    calls = []
    stop = OnePass(on_wait=lambda: requests.enqueue(document["registration_id"], lambda: document))

    @contextmanager
    def connected(args):
        assert args.expected_broker_url == ORIGIN
        assert args.expected_registration_id == document["registration_id"]
        assert args.expected_target == document["target"]
        assert personal_connector.read_private(args.setup) == document["setup"]
        directory = Path(args.state_directory)
        directory.mkdir(parents=True, mode=0o700)
        directory.parent.chmod(0o700)
        personal_connector.write_private(directory / "state.json", {"broker_url": ORIGIN,
            "registration_id": document["registration_id"], "name": document["name"]})
        ConnectorProfiles(args.registry_directory).remember(document["name"], directory, identifier=document["registration_id"])
        calls.append(args.connector_label)
        try:
            yield {"name": document["name"]}, object(), directory, None, ConnectorStatus({"name": document["name"]})
        finally:
            calls.append("closed")

    async def serve(*args, **kwargs):
        stop.stopped = True
        await asyncio.Event().wait()

    monkeypatch.setattr(personal_connector, "connected", connected)
    monkeypatch.setattr(personal_connector, "serve", serve)
    deadline = time.monotonic() + 2
    local.run_connectors(ORIGIN, data_directory, stop_event=stop, server_alive=lambda: time.monotonic() < deadline, poll_interval=0.001)
    assert calls == ["New lab cluster", "closed"]
    assert len(ConnectorProfiles(data_directory / "connectors-registry").list()) == 1
    assert not list(requests.directory.glob(".setup-*.json"))
    assert "synthetic-private-pairing-token" not in str(capsys.readouterr())


def test_server_shutdown_does_not_authenticate_saved_connectors(data_directory, monkeypatch):
    saved(data_directory, "agmn")
    monkeypatch.setattr(personal_connector, "connected", lambda _: pytest.fail("Stopped server triggered SSH"))
    local.run_connectors(ORIGIN, data_directory, server_alive=lambda: False)


def test_local_supervisor_propagates_selected_extensions_to_every_serving_worker(data_directory, monkeypatch):
    saved(data_directory, "agmn")
    received = []

    @contextmanager
    def connected(args):
        yield {"name": "agmn"}, object(), Path(args.state_directory), None, ConnectorStatus({"name": "agmn"})

    async def serve(state, session, directory, **kwargs):
        received.append(kwargs["extensions"])
        await asyncio.Event().wait()

    monkeypatch.setattr(personal_connector, "connected", connected)
    monkeypatch.setattr(personal_connector, "serve", serve)
    local.run_connectors(ORIGIN, data_directory, extensions=["storage", "environments"], stop_event=OnePass(), poll_interval=0.001)
    assert received == [("storage", "environments")]


def test_local_supervisor_rejects_unsupported_extension_hooks_before_any_ssh(data_directory, monkeypatch):
    from compute_relay import extensions
    saved(data_directory, "agmn")
    monkeypatch.setattr(personal_connector, "connected", lambda _: pytest.fail("Unsupported extension configuration triggered SSH"))
    monkeypatch.setattr(extensions, "import_module", lambda _: SimpleNamespace(execute=lambda: None))
    with pytest.raises(ValueError, match="native async worker hooks"):
        local.run_connectors(ORIGIN, data_directory, extensions=["storage"], stop_event=OnePass())


def test_same_host_with_distinct_ssh_accounts_and_workers_remains_separate_in_one_project(data_directory, monkeypatch, db):
    from compute_relay.control.models import ClusterRegistration, Connection, Project, User, WorkerIdentity
    owner = User.objects.create_user(username="same-host-owner")
    project = Project.objects.create(owner=owner, name="Shared science project")
    registry = ConnectorProfiles(data_directory / "connectors-registry")
    expected = []
    for username, maximum_cpus in (("scientist-a", 2), ("scientist-b", 16)):
        worker = WorkerIdentity.objects.create(owner=owner, name=username, token_hash=uuid4().hex * 2)
        configuration = {"ssh": {"host": "same.cluster.invalid", "user": username, "port": 22},
            "site_profile": {"maximum_cpus": maximum_cpus}}
        connection = Connection.objects.create(owner=owner, worker=worker, name="Lab", backend="slurm",
            configuration=configuration, revision_sha256=uuid4().hex * 2)
        project.connections.add(connection)
        registration = ClusterRegistration.objects.create(owner=owner, connection=connection,
            name="Lab", host="same.cluster.invalid", remote_user=username)
        directory = local.connector_directory(data_directory, registration.id)
        directory.mkdir(parents=True, mode=0o700)
        directory.parent.chmod(0o700)
        state = {"broker_url": ORIGIN, "registration_id": str(registration.id), "name": "Lab",
            "owner_id": str(owner.id), "worker_id": str(worker.id), "connection_id": str(connection.id),
            "connection": configuration, "site_profile": configuration["site_profile"]}
        personal_connector.write_private(directory / "state.json", state)
        registry.remember("Lab", directory, identifier=registration.id)
        expected.append((username, str(worker.id), str(connection.id), maximum_cpus))
    received, closed = [], []

    @contextmanager
    def connected(args):
        state = personal_connector.read_private(Path(args.state_directory) / "state.json")
        try:
            yield state, object(), Path(args.state_directory), None, ConnectorStatus(state, label=args.connector_label)
        finally:
            closed.append(state["worker_id"])

    async def serve(state, session, directory, **kwargs):
        received.append((state["connection"]["ssh"]["user"], state["worker_id"], state["connection_id"],
            state["site_profile"]["maximum_cpus"]))
        await asyncio.Event().wait()

    monkeypatch.setattr(personal_connector, "connected", connected)
    monkeypatch.setattr(personal_connector, "serve", serve)
    local.run_connectors(ORIGIN, data_directory, stop_event=OnePass(), poll_interval=0.001)
    assert sorted(received) == sorted(expected)
    assert set(closed) == {item[1] for item in expected}
    assert project.connections.count() == 2
    shortcuts = registry.list()
    assert len({item.name for item in shortcuts}) == len({item.state_directory for item in shortcuts}) == 2


def test_registration_names_are_safe_unique_and_idempotent(data_directory):
    directory = saved(data_directory, "existing")
    registry = ConnectorProfiles(data_directory / "connectors-registry")
    assert registry.remember("ignored new name", directory).name == "existing"
    other = saved(data_directory, "second")
    registry.remove("second")
    assert registry.remember("existing", other, identifier="01234567-abcd").name == "existing-01234567"
    registry.remove("existing-01234567")
    assert registry.remember("Lab\ncluster; $(unsafe)", other).name == "Lab-cluster-unsafe"
