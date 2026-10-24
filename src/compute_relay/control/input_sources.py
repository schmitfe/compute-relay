"""Input authority is an upload grant or an exact, currently readable artifact.

Content hashes identify bytes; they do not grant permission to those bytes.
Internal generated files are admitted only through the trusted Python API.
"""

from django.utils import timezone

from .auth import require_connection, require_credential_actor
from .errors import BrokerError
from .models import Artifact, Blob, InputSource, ProjectCredential, UploadGrant


def current_credential(credential):
    current = ProjectCredential.objects.select_related("project").filter(
        id=credential.id, project_id=credential.project_id, revoked=False,
        expires_at__gt=timezone.now()).first()
    if current is None:
        raise BrokerError("AUTH_REQUIRED", "Credential is invalid, revoked or expired.", 403)
    require_credential_actor(current, "jobs:read")
    return current


def authorize_input_artifact(credential, artifact):
    credential = current_credential(credential)
    if artifact.blob.project_id != credential.project_id or artifact.attempt.job.project_id != credential.project_id:
        raise BrokerError("OBJECT_MISSING", "No authorized input matches this descriptor.", 404)
    if "artifacts:read" not in credential.scopes:
        raise BrokerError("SCOPE_DENIED", "Reading an artifact input requires artifacts:read.", 403)
    require_connection(credential, artifact.attempt.connection_id)
    return artifact.blob


def authorize_source(credential, blob, artifact=None):
    if blob.project_id != credential.project_id:
        raise BrokerError("OBJECT_MISSING", "No authorized input matches this descriptor.", 404)
    if artifact is not None:
        if artifact.blob_id != blob.id:
            raise BrokerError("INPUT_SOURCE_CHANGED", "The sealed input source changed.", 409)
        authorize_input_artifact(credential, artifact)
    elif not UploadGrant.objects.filter(blob=blob).exists():
        raise BrokerError("INPUT_DENIED", "Upload these bytes explicitly or select a readable artifact.", 403)


def resolve_input_blob(credential, descriptor):
    blob = Blob.objects.filter(project_id=credential.project_id, sha256=descriptor["sha256"],
                               size_bytes=descriptor["size_bytes"]).first()
    if blob is None:
        raise BrokerError("OBJECT_MISSING", "Upload and verify every referenced file first.", 404)
    if UploadGrant.objects.filter(blob=blob).exists():
        return blob, None
    # Never substitute a merely matching output on a disallowed producer cluster.
    for artifact in Artifact.objects.select_related("blob", "attempt__job").filter(blob=blob).order_by("id"):
        try:
            authorize_input_artifact(credential, artifact)
        except BrokerError as exc:
            if exc.code in {"SCOPE_DENIED", "CONNECTION_DENIED"}:
                continue
            raise
        return blob, artifact
    raise BrokerError("INPUT_DENIED", "Upload these bytes explicitly or select a readable artifact.", 403)


def authorize_job_inputs(job, credential):
    credential = current_credential(credential)
    if job.project_id != credential.project_id:
        raise BrokerError("NOT_FOUND", "Job not found.", 404)
    for entry in job.files.select_related("blob", "source_artifact__attempt__job"):
        if entry.source_kind == "generated" and entry.source_artifact_id is None:
            if entry.blob.project_id != job.project_id:
                raise BrokerError("INPUT_SOURCE_CHANGED", "The sealed input source changed.", 409)
        elif entry.source_kind == "artifact" and entry.source_artifact_id:
            authorize_source(credential, entry.blob, entry.source_artifact)
        elif entry.source_kind == "upload" and entry.source_artifact_id is None:
            authorize_source(credential, entry.blob)
        else:
            raise BrokerError("INPUT_DENIED", "This input has no verified provenance; upload it in a new revision.", 403)


def seal_definition_inputs(project, kind, object_id, sources):
    InputSource.objects.bulk_create([InputSource(project=project, kind=kind, object_id=object_id,
        path=path, blob=blob, artifact=artifact) for path, (blob, artifact) in sources.items()])


def definition_inputs(credential, kind, object_id, descriptors):
    sources = {source.path: source for source in InputSource.objects.select_related("blob", "artifact__attempt__job").filter(
        project_id=credential.project_id, kind=kind, object_id=object_id)}
    result = {}
    for descriptor in descriptors:
        source = sources.get(descriptor["path"])
        if source is None or source.blob.sha256 != descriptor["sha256"] or source.blob.size_bytes != descriptor["size_bytes"]:
            raise BrokerError("INPUT_DENIED", "The sealed definition has no verified input source.", 403)
        authorize_source(credential, source.blob, source.artifact)
        result[source.path] = (source.blob, source.artifact)
    return result
