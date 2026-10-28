"""Saved definitions retain the same artifact authority as concrete jobs."""
from copy import deepcopy

import pytest

from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Artifact, Attempt, ExecutionRequest, Job
from compute_relay.control.storage import store_blob, store_uploaded_blob
from compute_relay.extensions.environments.models import EnvironmentBuild
from compute_relay.extensions.workflows.models import WorkflowRevision
from compute_relay.extensions.workflows import services as workflows
from test_support.unit_test_control import broker
from test_support.unit_test_environment_extension import context as environment_context
from test_support.unit_test_input_provenance import artifact_input


def wheel_artifact(context):
    # Synthetic receipt only: neither a scheduler nor the wheel is executed.
    job = Job.objects.create(project=context.project, credential=context.key, request={})
    execution = ExecutionRequest.objects.create(job=job, credential=context.key)
    attempt = Attempt.objects.create(execution_request=execution, job=job, connection=context.connection,
        grant=context.grant, reservation_cpu=0, reservation_gpu=0)
    blob = store_blob(context.project, b"synthetic wheel bytes")
    Artifact.objects.create(attempt=attempt, path="wheel.whl", blob=blob)
    context.document["requirements"] = [{"name": "example", "version": "1.0",
        "filename": "example-1.0-py3-none-any.whl", "sha256": blob.sha256, "size_bytes": blob.size_bytes}]
    return blob


def test_wheel_digest_without_artifact_scope_is_not_build_authority(environment_context):
    context = environment_context
    wheel_artifact(context)
    with pytest.raises(BrokerError) as exc:
        context.services.create_build(context.key, context.document)
    assert exc.value.code == "INPUT_DENIED"
    assert not EnvironmentBuild.objects.exists()


def test_sealed_wheel_rechecks_artifact_permission_after_build_creation(environment_context):
    context = environment_context
    blob = wheel_artifact(context)
    context.key.scopes += ["artifacts:read"]
    context.key.save(update_fields=["scopes"])
    created, _ = context.services.create_build(context.key, context.document)
    build = EnvironmentBuild.objects.get(id=created["id"])
    context.key.scopes.remove("artifacts:read")
    context.key.save(update_fields=["scopes"])
    # An independent copy cannot silently replace an already sealed binding.
    assert store_uploaded_blob(context.project, b"synthetic wheel bytes").id == blob.id
    with pytest.raises(BrokerError) as exc:
        context.services.prepare_build(build, context.key)
    assert exc.value.code == "SCOPE_DENIED"
    build.refresh_from_db()
    assert build.job_id is None


def test_sealed_workflow_direct_input_rechecks_producer_permission(broker):
    _, _, request = artifact_input(broker)
    broker.credential.scopes += ["workflows:write"]
    broker.credential.save(update_fields=["scopes"])
    document = {"name": "artifact-source", "nodes": [{"key": "one", "request": request}]}
    created, _ = workflows.create_revision(broker.credential, document)
    revision = WorkflowRevision.objects.get(id=created["id"])
    broker.credential.scopes.remove("artifacts:read")
    broker.credential.save(update_fields=["scopes"])
    store_uploaded_blob(broker.project, b"synthetic private artifact\n")
    with pytest.raises(BrokerError) as exc:
        workflows.start_run(broker.credential, revision)
    assert exc.value.code == "SCOPE_DENIED"
    assert not revision.runs.exists()


def test_build_definitions_and_build_instances_share_metadata_cap(environment_context, settings):
    context = environment_context
    settings.MAX_PROJECT_DEFINITIONS = 2
    context.services.create_build(context.key, context.document)
    with pytest.raises(BrokerError) as exc:
        context.services.create_build(context.key, deepcopy(context.document))
    assert exc.value.code == "METADATA_QUOTA"
    assert EnvironmentBuild.objects.count() == 1
