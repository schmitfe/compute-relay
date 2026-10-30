"""Registered runtime paths pin a core attempt even without a module build row."""

import pytest

from compute_relay.control import remote_cleanup, services
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Environment
from test_remote_cleanup import completed, request_cleanup
from test_support.unit_test_control import broker


@pytest.mark.parametrize("paths", [
    {"prefix": "/owned/attempt-directory/venv"},
    {"interpreter": "/owned/attempt-directory/venv/bin/python"},
    {"conda_executable": "/owned/attempt-directory/bin/conda"},
    {"module_init": "/owned/attempt-directory/init.sh"},
    {"module_paths": ["/owned/attempt-directory/modules"]},
    {"prefix": "/owned"},
    {"prefix": "/owned/elsewhere/../attempt-directory/venv"},
])
@pytest.mark.parametrize("other_connection", [False, True])
def test_environment_registered_after_cleanup_lease_blocks_dispatch(broker, paths, other_connection):
    job, attempt = completed(broker)
    directory = "/owned/attempt-directory"
    attempt.native_identity = {**attempt.native_identity, "work_dir": directory}
    attempt.save(update_fields=["native_identity"])
    connection = alias_connection(broker) if other_connection else broker.connection
    assert request_cleanup(broker, job) == {"queued": 1}
    lease = services.lease_work(broker.worker)["lease"]
    Environment.objects.create(owner=broker.owner, connection=connection, name="registered existing venv",
        revision_sha256="d" * 64, configuration={"kind": "venv", **paths})
    with pytest.raises(BrokerError) as denied:
        services.dispatch(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
            "manifest_sha256": job.manifest_sha256, "connection_revision_sha256": broker.connection.revision_sha256})
    assert denied.value.code == "CLEANUP_PROTECTED"


def test_environment_in_similarly_named_sibling_does_not_protect_attempt(broker):
    job, attempt = completed(broker)
    attempt.native_identity = {**attempt.native_identity, "work_dir": "/owned/attempt"}
    attempt.save(update_fields=["native_identity"])
    Environment.objects.create(owner=broker.owner, connection=broker.connection, name="sibling venv",
        revision_sha256="d" * 64, configuration={"kind": "venv", "prefix": "/owned/attempt-other/venv"})
    assert remote_cleanup.preview(broker.project, job)["eligible_count"] == 1


def alias_connection(broker, **ssh):
    from compute_relay.control.models import Connection
    identity = {"host": "cluster.example", "user": "researcher", "port": 22}
    broker.connection.configuration = {**broker.connection.configuration, "ssh": identity}
    broker.connection.save(update_fields=["configuration"])
    return Connection.objects.create(owner=broker.owner, worker=broker.worker, name="Another connector",
        backend="slurm", configuration={"ssh": {**identity, **ssh}, "remote_root": "/another/root"},
        revision_sha256="b" * 64)


@pytest.mark.parametrize("ssh,protected", [
    ({"host": "CLUSTER.EXAMPLE."}, True),
    ({"host": "other.example"}, False),
    ({"user": "other-user"}, False),
    ({"port": 2222}, False),
])
def test_environment_on_equivalent_connection_protects_same_remote_directory(broker, ssh, protected):
    job, attempt = completed(broker)
    attempt.native_identity = {**attempt.native_identity, "work_dir": "/owned/attempt"}
    attempt.save(update_fields=["native_identity"])
    alias = alias_connection(broker, **ssh)
    Environment.objects.create(owner=broker.owner, connection=alias, name="retained venv",
        revision_sha256="d" * 64, configuration={"prefix": "/owned/attempt/environment"})
    assert remote_cleanup.preview(broker.project, job)["eligible_count"] == int(not protected)


@pytest.mark.parametrize("path,protected", [
    ("/owned/attempt/shared", True), ("/owned/attempt", True),
    ("/owned/attempt-other", False), ("/owned", False),
])
def test_storage_locations_inside_attempt_are_retained_without_pinning_shared_work_children(broker, path, protected):
    from compute_relay.extensions.storage.models import StorageCapacityPool, StorageTarget
    job, attempt = completed(broker)
    attempt.native_identity = {**attempt.native_identity, "work_dir": "/owned/attempt"}
    attempt.save(update_fields=["native_identity"])
    alias = alias_connection(broker)
    pool = StorageCapacityPool.objects.create(owner=broker.owner, connection=alias, name="Shared work", probe_root=path)
    StorageTarget.objects.create(owner=broker.owner, project=broker.project, connection=alias, pool=pool,
        name="Registered location", root_alias="work", root=path, roles=["work"],
        profile_revision_sha256="d" * 64, connection_revision_sha256=alias.revision_sha256)
    assert remote_cleanup.preview(broker.project, job)["eligible_count"] == int(not protected)


def test_storage_location_registered_after_lease_blocks_final_cleanup_dispatch(broker):
    from compute_relay.extensions.storage.models import StorageCapacityPool
    job, attempt = completed(broker)
    attempt.native_identity = {**attempt.native_identity, "work_dir": "/owned/attempt"}
    attempt.save(update_fields=["native_identity"])
    alias = alias_connection(broker)
    assert request_cleanup(broker, job) == {"queued": 1}
    lease = services.lease_work(broker.worker)["lease"]
    StorageCapacityPool.objects.create(owner=broker.owner, connection=alias,
        name="Retained location", probe_root="/owned/attempt/data")
    with pytest.raises(BrokerError) as denied:
        services.dispatch(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
            "manifest_sha256": job.manifest_sha256, "connection_revision_sha256": broker.connection.revision_sha256})
    assert denied.value.code == "CLEANUP_PROTECTED"


@pytest.mark.parametrize("state", ["queued", "running"])
def test_environment_enrollment_cannot_race_approved_cleanup_on_equivalent_connection(broker, state):
    from django.test import Client
    from compute_relay.control.models import Attempt
    job, attempt = completed(broker)
    attempt.native_identity = {**attempt.native_identity, "work_dir": "/owned/attempt"}
    attempt.save(update_fields=["native_identity"])
    alias = alias_connection(broker)
    Attempt.objects.filter(id=attempt.id).update(remote_cleanup={"state": state})
    browser = Client()
    browser.force_login(broker.owner)
    response = browser.post(f"/clusters/{alias.id}/environments/new/", {
        "name": "Must not retain a deleting environment", "connection": str(alias.id), "kind": "venv",
        "interpreter": "/owned/attempt/environment/bin/python", "prefix": "/owned/attempt/environment"})
    assert response.status_code == 409 and b"CLEANUP_IN_PROGRESS" in response.content
    assert not Environment.objects.filter(connection=alias).exists()


def test_changed_binding_is_explicitly_ineligible_for_remote_cleanup(broker):
    job, attempt = completed(broker)
    broker.connection.revision_sha256 = "d" * 64
    broker.connection.save(update_fields=["revision_sha256"])
    preview = remote_cleanup.preview(broker.project, job)
    assert preview["eligible_count"] == 0
    assert "manual remote cleanup" in preview["rows"][0]["reason"]


def test_pending_storage_confirmation_protects_the_worker_resolved_root_at_dispatch(broker):
    from datetime import timedelta
    from django.utils import timezone
    from compute_relay.control.models import ConnectorTask
    from compute_relay.extensions.storage.models import StorageTargetRegistration
    job, attempt = completed(broker)
    attempt.native_identity = {**attempt.native_identity, "work_dir": "/owned/attempt"}
    attempt.save(update_fields=["native_identity"])
    assert request_cleanup(broker, job) == {"queued": 1}
    lease = services.lease_work(broker.worker)["lease"]
    task = ConnectorTask.objects.create(owner=broker.owner, connection=broker.connection, kind="mkdir", state="succeeded",
        connection_revision=broker.connection.revision_sha256, expires_at=timezone.now() + timedelta(minutes=1),
        request={"path": "/parent-alias", "name": "data", "parents": True},
        result={"path": "/owned/attempt/data", "parent": "/owned/attempt", "ok": True, "writable": True, "created": False})
    StorageTargetRegistration.objects.create(owner=broker.owner, project=broker.project, connection=broker.connection,
        task=task, name="Pending retained data", root="/parent-alias/data", roles=["data"],
        profile_revision_sha256="d" * 64, connection_revision_sha256=broker.connection.revision_sha256)
    with pytest.raises(BrokerError) as denied:
        services.dispatch(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
            "manifest_sha256": job.manifest_sha256, "connection_revision_sha256": broker.connection.revision_sha256})
    assert denied.value.code == "CLEANUP_PROTECTED"
