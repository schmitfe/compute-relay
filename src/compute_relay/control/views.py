"""Small HTTP boundary. Agent bearer credentials never authorize human decisions."""

import base64
import binascii
import functools
import json
import logging
from uuid import UUID

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt, csrf_protect, ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters
from jsonschema.exceptions import ValidationError as SchemaValidationError

from compute_relay.domain.validation import strict_json, relative_path
from . import services
from .auth import human_permission, project_credential, worker_identity, require_connection, connection_allowed
from .errors import BrokerError
from .models import Artifact, Attempt, IdempotencyRecord, Job, Project
from .storage import read_blob, store_blob

logger = logging.getLogger(__name__)


def boundary(*methods, exempt=True):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(request, *args, **kwargs):
            try:
                if request.method not in methods:
                    raise BrokerError("METHOD_NOT_ALLOWED", "Method is not supported.", 405)
                response = fn(request, *args, **kwargs)
                if isinstance(response, tuple):
                    return JsonResponse(response[0], status=response[1])
                return response if isinstance(response, HttpResponse) else JsonResponse(response)
            except BrokerError as exc:
                return JsonResponse({"error": {"code": exc.code, "message": exc.message, "details": exc.details}}, status=exc.status)
            except ObjectDoesNotExist:
                return JsonResponse({"error": {"code": "NOT_FOUND", "message": "Object not found."}}, status=404)
            except (ValueError, TypeError, KeyError, DjangoValidationError, SchemaValidationError, binascii.Error):
                return JsonResponse({"error": {"code": "INVALID_REQUEST", "message": "Request contains invalid or missing fields."}}, status=400)
        return csrf_exempt(wrapped) if exempt else csrf_protect(wrapped)
    return decorate


def body(request, maximum=2 * 1024 * 1024):
    if request.content_type != "application/json":
        raise BrokerError("CONTENT_TYPE", "Send application/json.", 415)
    if int(request.headers.get("Content-Length", 0)) > maximum:
        raise BrokerError("BODY_LIMIT", "Request exceeds the size limit.", 413)
    raw = request.body
    if len(raw) > maximum:
        raise BrokerError("BODY_LIMIT", "Request exceeds the size limit.", 413)
    document = strict_json(raw)
    if not isinstance(document, dict):
        raise BrokerError("INVALID_REQUEST", "Expected a JSON object.")
    return document


def scope(request, permission, *, project_id=None, job_id=None, attempt_id=None):
    credential = project_credential(request, permission)
    if project_id and str(credential.project_id) != str(project_id):
        raise BrokerError("NOT_FOUND", "Project not found.", 404)
    if job_id:
        job = Job.objects.get(id=job_id, project=credential.project)
        require_connection(credential, job.request["connection_id"])
        return credential, job
    if attempt_id:
        attempt = Attempt.objects.get(id=attempt_id, job__project=credential.project)
        require_connection(credential, attempt.connection_id)
        return credential, attempt
    return credential, credential.project


def idem(request, credential, operation, document, action):
    return services.idempotent(credential.project, operation, request.headers.get("Idempotency-Key"), document, action)


@boundary("GET")
def health(request):
    result = {"status": "ok", "profile": settings.PROFILE, "version": "0.1.0.dev0"}
    if settings.BROKER_LOCAL_RUNTIME == "1":
        for key in ("installation_id", "instance_id"):
            value = getattr(settings, "BROKER_" + key.upper(), None)
            if value is not None:
                result[key] = str(value)
    return result


@boundary("GET")
def project_context(request):
    credential = project_credential(request)
    payload = {"schema_version": "1.0",
        "project": {"id": str(credential.project_id), "name": credential.project.name},
        "credential": {"id": str(credential.id), "scopes": credential.scopes,
                       "execution_mode": credential.execution_mode}}
    if "jobs:read" in credential.scopes:
        from .resource_api import compute_context
        payload["compute"] = compute_context(credential)
        payload["allowance"] = services.allowance(credential.project)
    response = JsonResponse(payload)
    response["Cache-Control"] = "no-store"
    response["Vary"] = "Authorization"
    return response


@boundary("GET")
def connections(request):
    credential, _ = scope(request, "jobs:read")
    from .resource_api import compute_context
    return compute_context(credential)


@boundary("GET")
def allowance(request, project_id):
    credential, project = scope(request, "jobs:read", project_id=project_id)
    from .resource_api import compute_context
    return {**services.allowance(project), "compute": compute_context(credential)}


@boundary("POST")
def objects(request, project_id):
    credential, project = scope(request, "objects:write", project_id=project_id)
    document = body(request)
    relative_path(document["path"])
    if document["path"].startswith("_broker/"):
        raise BrokerError("PATH_RESERVED", "This path is reserved for generated files.")
    data = base64.b64decode(document["content_base64"], validate=True)
    from .storage import store_uploaded_blob
    blob = store_uploaded_blob(project, data)
    return {"path": document["path"], "sha256": blob.sha256, "size_bytes": blob.size_bytes,
            "executable": document.get("executable", False)}, 201


@boundary("POST")
def jobs(request):
    credential, _ = scope(request, "jobs:write")
    document = body(request)
    result = idem(request, credential, "jobs:create", document, lambda: services.create_job(credential, document))
    from .input_sources import authorize_job_inputs
    if result[1] < 400:
        authorize_job_inputs(Job.objects.get(id=result[0]["id"]), credential)
    return result


@boundary("GET")
def job_detail(request, job_id):
    credential, job = scope(request, "jobs:read", job_id=job_id)
    from .input_sources import authorize_job_inputs
    authorize_job_inputs(job, credential)
    return services.serialize_job(job)


@boundary("GET")
def package(request, job_id):
    credential, job = scope(request, "jobs:read", job_id=job_id)
    if job.state == "archived":
        raise BrokerError("JOB_ARCHIVED", "The executable package was archived. Create a fresh job.", 410)
    from .input_sources import authorize_job_inputs
    authorize_job_inputs(job, credential)
    return services.serialize_job(job, package=True)


@boundary("POST")
def prepare(request, job_id):
    credential, job = scope(request, "jobs:write", job_id=job_id)
    from .input_sources import authorize_job_inputs
    authorize_job_inputs(job, credential)
    document = body(request)
    if document:
        raise BrokerError("INVALID_REQUEST", "Preparation accepts an empty object.")
    key = request.headers.get("Idempotency-Key")
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise BrokerError("IDEMPOTENCY_REQUIRED", "Supply an Idempotency-Key of at most 128 characters.")
    operation = f"{job_id}:prepare"
    existing = IdempotencyRecord.objects.filter(project=credential.project, operation=operation, key=key).first()
    if existing:
        if existing.request_sha256 != services.digest(document):
            raise BrokerError("IDEMPOTENCY_CONFLICT", "The key was used with different content.", 409)
        if existing.compacted:
            raise BrokerError("IDEMPOTENCY_RETIRED", "This request was already processed; its response was retired.", 409)
        return existing.response, existing.status
    # File reservations must commit outside the idempotency transaction, so a
    # crash cannot roll back their accounting while leaving published bytes.
    # Preparation is itself immutable/idempotent under the per-job lock.
    result = services.prepare_job(job)
    return idem(request, credential, operation, document, lambda: result)


@boundary("POST")
def revisions(request, job_id):
    credential, job = scope(request, "jobs:write", job_id=job_id)
    document = body(request)
    from .input_sources import authorize_job_inputs
    result = idem(request, credential, f"{job_id}:revise", document, lambda: services.create_job(credential, document, previous=job))
    if result[1] < 400:
        authorize_job_inputs(Job.objects.get(id=result[0]["id"]), credential)
    return result


@boundary("POST")
def execution_requests(request, job_id):
    credential, job = scope(request, "jobs:execute", job_id=job_id)
    document = body(request)
    return idem(request, credential, f"{job_id}:execute", document, lambda: services.request_execution(job, credential))


@boundary("POST")
def cancel_execution_request(request, job_id, execution_id):
    credential, job = scope(request, "jobs:cancel", job_id=job_id)
    document = body(request)
    if document:
        raise BrokerError("INVALID_REQUEST", "Cancellation accepts an empty object.")
    execution = job.execution_requests.get(id=execution_id)
    return idem(request, credential, f"{execution_id}:cancel", document,
                lambda: services.cancel_execution_request(execution))


@boundary("POST", exempt=False)
def decisions(request, job_id):
    # Check human authority before parsing the body or any project token.
    if not request.user.is_authenticated:
        raise BrokerError("HUMAN_REQUIRED", "A human review session is required.", 403)
    job = Job.objects.get(id=job_id)
    human_permission(request.user, job.project, "approver")
    document = body(request)
    if not isinstance(document.get("comment", ""), str):
        raise BrokerError("INVALID_REQUEST", "Comment must be text.")
    return services.idempotent(job.project, f"{job_id}:decision:{request.user.id}", request.headers.get("Idempotency-Key"), document,
                               lambda: services.decide(job, request.user, document))


@boundary("POST")
def cancel(request, attempt_id):
    credential, attempt = scope(request, "jobs:cancel", attempt_id=attempt_id)
    document = body(request)
    return idem(request, credential, f"{attempt_id}:cancel", document, lambda: services.cancel_attempt(attempt))


@boundary("GET")
def logs(request, attempt_id):
    _, attempt = scope(request, "jobs:read", attempt_id=attempt_id)
    if "stream" in request.GET:
        stream = request.GET["stream"]
        cursor = int(request.GET.get("cursor", "0"))
        limit = int(request.GET.get("limit", "65536"))
        if stream not in {"stdout", "stderr"} or not 0 <= cursor <= 2**53 - 1 or not 1 <= limit <= 65536:
            raise BrokerError("INVALID_REQUEST", "Invalid log stream, cursor or limit.")
        content = getattr(attempt, stream).encode("utf-8")
        chunk = content[cursor:cursor + limit]
        return {"stream": stream, "content_base64": base64.b64encode(chunk).decode("ascii"),
                "text": chunk.decode("utf-8", errors="replace"), "cursor": cursor,
                "next_cursor": min(len(content), cursor + len(chunk)), "eof": cursor + len(chunk) >= len(content)}
    return {"stdout": attempt.stdout, "stderr": attempt.stderr}


@boundary("GET")
def artifacts(request, attempt_id):
    _, attempt = scope(request, "artifacts:read", attempt_id=attempt_id)
    return {"artifacts": [{"id": str(a.id), "path": a.path, "sha256": a.blob.sha256, "size_bytes": a.blob.size_bytes}
                          for a in attempt.artifacts.select_related("blob").order_by("path")]}


@boundary("GET")
def artifact_content(request, artifact_id):
    credential, _ = scope(request, "artifacts:read")
    artifact = Artifact.objects.select_related("blob").get(id=artifact_id, attempt__job__project=credential.project)
    require_connection(credential, artifact.attempt.connection_id)
    response = HttpResponse(read_blob(artifact.blob), content_type="application/octet-stream")
    response["Content-Disposition"] = 'attachment; filename="artifact.bin"'
    return response


@boundary("POST")
def lease(request):
    worker = worker_identity(request)
    document = body(request, maximum=32768)
    if not isinstance(document, dict) or set(document) - {"core_only", "in_progress", "observation_batch"} or type(document.get("core_only", False)) is not bool:
        raise BrokerError("INVALID_REQUEST", "Invalid worker polling options.")
    batch = document.get("observation_batch", 1)
    if type(batch) is not int or not 1 <= batch <= 64:
        raise BrokerError("INVALID_REQUEST", "Observation batch must be from 1 through 64.")
    excluded = document.get("in_progress", [])
    if not isinstance(excluded, list) or len(excluded) > 8 * batch:
        raise BrokerError("INVALID_REQUEST", "Too many in-progress operations.")
    try:
        excluded = [str(UUID(value)) for value in excluded]
    except (ValueError, TypeError, AttributeError):
        raise BrokerError("INVALID_REQUEST", "Invalid in-progress attempt.") from None
    core = services.lease_work(worker, exclude_attempt_ids=excluded, observation_batch=batch)
    if core.get("lease") and core["lease"]["operation"] in {"cancel", "reconcile"}:
        return core
    try:
        from compute_relay.extensions import before_lease
        before_lease(worker)
    except Exception:
        logger.warning("Extension dispatch deferred; core work remains available")
    if core.get("lease") is not None or document.get("core_only", False):
        return core
    from .cluster_services import lease_discovery
    discovery = lease_discovery(worker)
    if discovery:
        return discovery
    try:
        from compute_relay.extensions import lease_extension_work
        extension_work = lease_extension_work(worker)
        if extension_work:
            return extension_work
    except Exception:
        logger.warning("Extension maintenance deferred; core work remains available")
    return services.lease_work(worker, exclude_attempt_ids=excluded, observation_batch=batch)


@boundary("GET")
def capabilities(request):
    credential, _ = scope(request, "jobs:read")
    from compute_relay.extensions import control_names
    return {"api_version": "1.0", "mcp_transports": ["stdio"],
            "enabled_extensions": list(control_names()), "scopes": credential.scopes}


@boundary("POST")
def worker_profiles(request):
    from .cluster_services import publish_profiles
    return publish_profiles(worker_identity(request), body(request))


@boundary("POST")
def discovery_report(request, connection_id):
    from .cluster_services import report_discovery
    return report_discovery(worker_identity(request), connection_id, body(request, maximum=2 * 1024 * 1024))


@boundary("POST")
def dispatch(request, attempt_id):
    return services.dispatch(worker_identity(request), attempt_id, body(request))


@boundary("POST")
def renew_lease(request, attempt_id):
    return services.renew_lease(worker_identity(request), attempt_id, body(request))


@boundary("POST")
def report(request, attempt_id):
    return services.report(worker_identity(request), attempt_id, body(request, maximum=16 * 1024 * 1024))


@csrf_protect
@ensure_csrf_cookie
@sensitive_post_parameters("password")
def signin(request):
    if request.user.is_authenticated:
        return redirect("/")
    error = ""
    if request.method == "POST":
        user = authenticate(request, username=request.POST.get("username"), password=request.POST.get("password"))
        if user is not None:
            login(request, user)
            return redirect("/")
        error = "Sign-in details were not recognized."
    response = render(request, "broker/login.html", {"error": error,
        "password_free": getattr(settings, "BROKER_LOCAL_AUTH_MODE", "password") == "none"})
    response["Cache-Control"] = "no-store, private"
    return response


@csrf_protect
def signout(request):
    if request.method == "POST":
        logout(request)
    return redirect("/login/")


def dashboard(request):
    if not request.user.is_authenticated:
        return redirect("/login/")
    from .local_auth import current_user_active
    if not current_user_active(request.user):
        return HttpResponse("The active local owner is required.", status=403)
    projects = Project.objects.filter(owner=request.user)
    return render(request, "broker/dashboard.html", {"projects": projects,
        "jobs": Job.objects.with_summary().filter(project__in=projects).order_by("-created_at", "-id")[:100]})


@csrf_protect
def review(request, job_id):
    if not request.user.is_authenticated:
        return redirect("/login/")
    try:
        job = Job.objects.get(id=job_id)
        human_permission(request.user, job.project, "approver")
        error = ""
        if request.method == "POST":
            action = request.POST.get("decision")
            if action not in {"approve", "reject", "cancel"}:
                raise BrokerError("INVALID_REQUEST", "Choose approval, rejection or cancellation.")
            document = {"execution_request_id": request.POST.get("execution_request_id"), "manifest_sha256": request.POST.get("manifest_sha256"),
                        "approved": action == "approve", "comment": request.POST.get("comment", "")}
            try:
                if action == "cancel":
                    execution = job.execution_requests.get(id=document["execution_request_id"])
                    services.idempotent(job.project, f"{job_id}:cancel-review:{request.user.id}",
                        request.POST.get("idempotency_key"), document, lambda: services.cancel_execution_request(execution))
                else:
                    services.idempotent(job.project, f"{job_id}:decision:{request.user.id}", request.POST.get("idempotency_key"), document,
                                        lambda: services.decide(job, request.user, document))
                return redirect(f"/review/{job_id}/")
            except BrokerError as exc:
                error = exc.message
        import uuid
        from .job_diagnostics import build_attempt_diagnostics
        detail = services.serialize_job(job, package=True)
        response = render(request, "broker/review.html", {"job": job, "detail": detail, "manifest_text": json.dumps(job.manifest, indent=2),
                                                      "attempt_diagnostics": [build_attempt_diagnostics(a) for a in job.attempts.select_related("connection").order_by("-created_at")],
                                                      "allowance": services.allowance(job.project), "error": error, "idempotency_key": str(uuid.uuid4())})
        response["Cache-Control"] = "no-store, private"
        response["Pragma"] = "no-cache"
        return response
    except (BrokerError, ObjectDoesNotExist):
        return HttpResponse("Review is unavailable for this account.", status=403, content_type="text/plain")
