"""Project-scoped resumable transfers, fenced worker I/O, and collection recovery."""

import base64

from django.db import transaction

from . import services
from .auth import worker_identity
from .errors import BrokerError
from .models import Artifact, Attempt, Blob
from .storage import begin_transfer, blob_chunk, transfer_chunk
from .views import body, boundary, scope


@boundary("POST")
def uploads(request, project_id):
    credential, project = scope(request, "objects:write", project_id=project_id)
    data = body(request, maximum=4096)
    if set(data) != {"sha256", "size_bytes"}:
        raise ValueError("Invalid transfer descriptor")
    return begin_transfer(project, data["sha256"], data["size_bytes"], "upload:" + str(credential.id), uploaded=True)


@boundary("POST")
def upload_chunk(request, project_id, transfer_id):
    credential, project = scope(request, "objects:write", project_id=project_id)
    data = body(request)
    if "offset" not in data or set(data) - {"offset", "content_base64", "finish"}:
        raise ValueError("Invalid upload chunk")
    if type(data.get("finish", False)) is not bool:
        raise ValueError("Invalid finish flag")
    return transfer_chunk(project, transfer_id, "upload:" + str(credential.id), data["offset"],
        base64.b64decode(data.get("content_base64", ""), validate=True), finish=data.get("finish", False), uploaded=True)


@boundary("POST")
def input_chunk(request, attempt_id):
    worker = worker_identity(request)
    data = body(request, maximum=4096)
    if set(data) != {"lease_id", "generation", "path", "offset"}:
        raise ValueError("Invalid input chunk request")
    with transaction.atomic():
        attempt = services.checked_lease(worker, attempt_id, data)
        if attempt.operation != "submit":
            raise BrokerError("INPUT_DENIED", "Input transfer requires a submission lease.", 403)
        blob = attempt.job.files.select_related("blob").get(path=data["path"]).blob
    content = blob_chunk(blob, data["offset"])
    return {"offset": data["offset"], "content_base64": base64.b64encode(content).decode("ascii")}


@boundary("POST")
def output_chunk(request, attempt_id):
    worker = worker_identity(request)
    data = body(request)
    required = {"lease_id", "generation", "path"}
    if "transfer_id" in data:
        if not (required | {"transfer_id", "offset"}) <= set(data) or set(data) - (required | {"transfer_id", "offset", "content_base64", "finish"}):
            raise ValueError("Invalid output chunk")
    elif set(data) != required | {"sha256", "size_bytes"}:
        raise ValueError("Invalid output descriptor")
    with transaction.atomic():
        attempt = services.checked_lease(worker, attempt_id, data)
        path = data["path"]
        if attempt.operation not in {"observe", "cancel"} or path not in attempt.job.request["outputs"]:
            raise BrokerError("ARTIFACT_DENIED", "Output is not authorized by this lease.", 403)
        project = attempt.job.project
    binding = "output:" + str(attempt_id) + ":" + path
    if "transfer_id" not in data:
        result = begin_transfer(project, data["sha256"], data["size_bytes"], binding)
    else:
        if type(data.get("finish", False)) is not bool:
            raise ValueError("Invalid finish flag")
        result = transfer_chunk(project, data["transfer_id"], binding, data["offset"],
            base64.b64decode(data.get("content_base64", ""), validate=True), finish=data.get("finish", False))
    if result["complete"]:
        with transaction.atomic():
            attempt = services.checked_lease(worker, attempt_id, data)
            blob = Blob.objects.get(project=project, sha256=result["sha256"], size_bytes=result["size_bytes"])
            artifact, created = Artifact.objects.get_or_create(attempt=attempt, path=path, defaults={"blob": blob})
            if not created and artifact.blob_id != blob.id:
                raise BrokerError("ARTIFACT_CHANGED", "Final artifact bytes changed.", 409)
    return result


@boundary("POST")
def retry_collection(request, attempt_id):
    _, attempt = scope(request, "jobs:write", attempt_id=attempt_id)
    if body(request):
        raise ValueError("Retry accepts an empty object")
    return services.retry_collection(attempt)
