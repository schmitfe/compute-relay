"""Permanent corrected regressions for review A01; no external execution."""
from copy import deepcopy
import base64
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.auth import token_hash
from compute_relay.control.errors import BrokerError
from compute_relay.control.input_sources import authorize_job_inputs
from compute_relay.control.models import Artifact, Connection, Environment, Job, ProjectCredential, UploadGrant
from compute_relay.control.storage import store_blob, store_uploaded_blob
from test_control import broker, budgeted, execution, prepared_job


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


def narrow(context, target, *, artifacts=False):
    return ProjectCredential.objects.create(project=context.project, issued_by=context.owner, name="restricted",
        token_hash=token_hash("restricted"), prefix="restricted", expires_at=timezone.now() + timedelta(days=1),
        scopes=["jobs:read", "jobs:write", "jobs:execute", "objects:write"] + (["artifacts:read"] if artifacts else []),
        allowed_connection_ids=[str(target.id)])


@pytest.mark.parametrize("artifacts", [False, True])
def test_raw_digest_cannot_launder_an_unreadable_producer(broker, artifacts):
    artifact, target, request = artifact_input(broker)
    key = narrow(broker, target, artifacts=artifacts)
    before = Job.objects.count()
    with pytest.raises(BrokerError) as exc:
        services.create_job(key, request)
    assert exc.value.code == "INPUT_DENIED"
    assert Job.objects.count() == before
    assert not UploadGrant.objects.filter(blob=artifact.blob).exists()


def test_package_and_revisions_recheck_exact_artifact_source(broker):
    artifact, target, request = artifact_input(broker)
    created, _ = services.create_job(broker.credential, request)
    job = Job.objects.get(id=created["id"])
    services.prepare_job(job)
    source = job.files.get(path="main.py")
    assert source.source_kind == "artifact" and source.source_artifact_id == artifact.id
    broker.credential.scopes.remove("artifacts:read")
    broker.credential.save(update_fields=["scopes"])
    client = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    for suffix in ("", "/package"):
        response = client.get(f"/api/v1/jobs/{job.id}{suffix}")
        assert response.status_code == 403
        assert "synthetic private artifact" not in response.content.decode()
    # An independent explicit upload authorizes new copies, not the old binding.
    assert store_uploaded_blob(broker.project, b"synthetic private artifact\n").id == artifact.blob_id
    with pytest.raises(BrokerError):
        authorize_job_inputs(job, broker.credential)
    copied, _ = services.create_job(broker.credential, request)
    assert Job.objects.get(id=copied["id"]).files.get(path="main.py").source_kind == "upload"


def test_explicit_deduplicated_upload_is_an_independent_authorized_copy(broker):
    artifact, target, request = artifact_input(broker)
    key = narrow(broker, target)
    blob = store_uploaded_blob(broker.project, b"synthetic private artifact\n")
    assert blob.id == artifact.blob_id
    created, _ = services.create_job(key, request)
    job = Job.objects.get(id=created["id"])
    authorize_job_inputs(job, key)
    assert job.files.get().source_kind == "upload"


def test_internal_generated_bytes_never_become_agent_input_authority(broker):
    job = prepared_job(broker)
    generated = job.files.filter(source_kind="generated").first()
    assert generated.source_kind == "generated"
    assert not UploadGrant.objects.filter(blob=generated.blob).exists()
    document = deepcopy(job.request)
    document["files"][0].update(sha256=generated.blob.sha256, size_bytes=generated.blob.size_bytes)
    with pytest.raises(BrokerError) as exc:
        services.create_job(broker.credential, document)
    assert exc.value.code == "INPUT_DENIED"


def test_create_idempotency_replay_cannot_return_now_unreadable_input(broker):
    _, _, request = artifact_input(broker)
    client = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    response = client.post("/api/v1/jobs", request, content_type="application/json", HTTP_IDEMPOTENCY_KEY="artifact-create")
    assert response.status_code == 201
    broker.credential.scopes.remove("artifacts:read")
    broker.credential.save(update_fields=["scopes"])
    replay = client.post("/api/v1/jobs", request, content_type="application/json", HTTP_IDEMPOTENCY_KEY="artifact-create")
    assert replay.status_code == 403


def test_lookup_does_not_confirm_an_unreadable_artifact_and_upload_grants_copy(broker):
    artifact, target, request = artifact_input(broker)
    narrow(broker, target)
    client = Client(HTTP_AUTHORIZATION="Bearer restricted")
    lookup = {"objects": [{"sha256": artifact.blob.sha256, "size_bytes": artifact.blob.size_bytes}]}
    path = f"/api/v1/projects/{broker.project.id}/objects/lookup"
    response = client.post(path, lookup, content_type="application/json")
    assert response.status_code == 200 and response.json()["present"] == []
    uploaded = client.post(f"/api/v1/projects/{broker.project.id}/objects", {
        "path": "main.py", "content_base64": base64.b64encode(b"synthetic private artifact\n").decode()},
        content_type="application/json")
    assert uploaded.status_code == 201
    assert uploaded.json()["sha256"] == artifact.blob.sha256
    assert UploadGrant.objects.filter(blob=artifact.blob).exists()
    response = client.post(path, lookup, content_type="application/json")
    assert response.json()["present"] == lookup["objects"]


def test_revision_resolves_its_own_explicit_copies_without_reading_previous_artifact(broker):
    _, target, request = artifact_input(broker)
    created, _ = services.create_job(broker.credential, request)
    key = narrow(broker, target)
    client = Client(HTTP_AUTHORIZATION="Bearer restricted")
    endpoint = f"/api/v1/jobs/{created['id']}/revisions"
    denied = client.post(endpoint, request, content_type="application/json", HTTP_IDEMPOTENCY_KEY="revise")
    assert denied.status_code == 403
    store_uploaded_blob(broker.project, b"synthetic private artifact\n")
    copied = client.post(endpoint, request, content_type="application/json", HTTP_IDEMPOTENCY_KEY="revise")
    assert copied.status_code == 201
    job = Job.objects.get(id=copied.json()["id"])
    assert job.previous_id == created["id"] or str(job.previous_id) == created["id"]
    authorize_job_inputs(job, key)
