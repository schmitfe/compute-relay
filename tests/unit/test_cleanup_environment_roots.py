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
def test_environment_registered_after_cleanup_lease_blocks_dispatch(broker, paths):
    job, attempt = completed(broker)
    directory = "/owned/attempt-directory"
    attempt.native_identity = {**attempt.native_identity, "work_dir": directory}
    attempt.save(update_fields=["native_identity"])
    assert request_cleanup(broker, job) == {"queued": 1}
    lease = services.lease_work(broker.worker)["lease"]
    Environment.objects.create(owner=broker.owner, connection=broker.connection, name="registered existing venv",
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
