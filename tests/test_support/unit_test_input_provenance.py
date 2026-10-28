from copy import deepcopy
from compute_relay.control.models import Artifact, Connection, Environment
from compute_relay.control.storage import store_blob
from test_support.unit_test_control import budgeted, execution, prepared_job

def artifact_input(context):
    producer = prepared_job(context)
    budgeted(context)
    attempt = execution(context, producer).attempt
    blob = store_blob(context.project, b"synthetic private artifact\n")
    artifact = Artifact.objects.create(attempt=attempt, path="private.txt", blob=blob)
    target = Connection.objects.create(owner=context.owner, worker=context.worker, name="second", backend="fake",
        configuration={"site_profile": {"automatic_execution_allowed": True}}, revision_sha256="d" * 64)
    environment = Environment.objects.create(owner=context.owner, connection=target, name="second-python",
        configuration={"interpreter": "/usr/bin/python3"}, revision_sha256="f" * 64)
    context.project.connections.add(target)
    context.project.environments.add(environment)
    request = deepcopy(producer.request)
    request["connection_id"] = str(target.id)
    request["environment"] = {"id": str(environment.id), "revision_sha256": environment.revision_sha256}
    request["files"] = [{"path": "main.py", "sha256": blob.sha256, "size_bytes": blob.size_bytes, "executable": False}]
    return artifact, target, request
