"""Optional environment builds reuse core jobs, approvals and accounting."""

import hashlib
import posixpath
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from compute_relay.backends.environments import render_environment
from compute_relay.control import services as core
from compute_relay.control.auth import human_permission, require_connection
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Attempt, Blob, Environment, Grant, Job, Project, ProjectCredential
from compute_relay.control.storage import store_blob
from compute_relay.domain.validation import validate

from .models import EnvironmentBuild, EnvironmentBuildPolicy, EnvironmentSpec
from .rendering import normalize_request, render_build_files, requirements_text, validate_policy


def _bootstrap(base):
    configuration = base.configuration
    kind = configuration.get("kind", "modules" if configuration.get("modules") else "system")
    if kind != "system":
        raise BrokerError("ENVIRONMENT_BASE_UNSUPPORTED", "This builder requires a standalone system Python recipe; module, Conda and virtual-environment bootstraps are not supported yet.", 403)
    try:
        render_environment(configuration, "python3")
    except ValueError as exc:
        raise BrokerError("ENVIRONMENT_BASE_UNSUPPORTED", "Select an approved system Python recipe for this builder.", 403) from exc


def _credential(credential, project, *, execute=False):
    if credential is None:
        raise BrokerError("ENVIRONMENT_BUILD_DENIED", "An environment-build credential is required.", 403)
    credential = ProjectCredential.objects.get(id=credential.id)
    required = {"environments:build", "jobs:write"} | ({"jobs:execute"} if execute else set())
    if credential.project_id != project.id or credential.revoked or credential.expires_at <= timezone.now() or not required <= set(credential.scopes):
        raise BrokerError("ENVIRONMENT_BUILD_DENIED", "A current project key with environment-build and job scopes is required.", 403)
    return credential


def _policy(project):
    policy = EnvironmentBuildPolicy.objects.filter(project=project).first()
    if policy is None:
        raise BrokerError("ENVIRONMENT_BUILDS_DISABLED", "A human must configure the project's environment-build policy.", 403)
    validate_policy(policy.configuration)
    if not policy.configuration["enabled"]:
        raise BrokerError("ENVIRONMENT_BUILDS_DISABLED", "Environment builds are disabled for this project.", 403)
    return policy


def _current_build_policy(build):
    policy = _policy(build.spec.project)
    if policy.id != build.policy_id or policy.revision != build.policy_revision or core.digest(policy.configuration) != build.policy_sha256:
        raise BrokerError("ENVIRONMENT_POLICY_CHANGED", "Build policy changed; create and review a fresh build.", 409)
    document = build.spec.document
    base = core.bound_environment(build.spec.project, {"connection_id": str(build.spec.connection_id), "environment": document["base_environment"]})
    _bootstrap(base)
    if str(base.id) not in policy.configuration["allowed_base_environment_ids"]:
        raise BrokerError("ENVIRONMENT_BASE_DENIED", "The bootstrap environment is outside the build policy.", 403)
    if document["connection_revision_sha256"] != build.spec.connection.revision_sha256:
        raise BrokerError("REVISION_CHANGED", "Connection changed; create a fresh build.", 409)
    return policy


def update_policy(project, user, configuration):
    human_permission(user, project, "project_admin")
    validate_policy(configuration)
    bases = set(configuration["allowed_base_environment_ids"])
    if set(str(e.id) for e in project.environments.filter(id__in=bases, connection_id__isnull=False)) != bases:
        raise BrokerError("ENVIRONMENT_BASE_DENIED", "Select bootstrap environments already bound to this project and a cluster.", 403)
    for base in project.environments.filter(id__in=bases):
        _bootstrap(base)
    with transaction.atomic():
        Project.objects.select_for_update().get(id=project.id)
        policy, created = EnvironmentBuildPolicy.objects.select_for_update().get_or_create(project=project)
        policy.configuration = configuration
        policy.revision = policy.revision if created else policy.revision + 1
        policy.updated_by = user
        policy.updated_at = timezone.now()
        policy.save()
        core.audit(project, user.id, "environment.policy.updated", policy.id, {"revision": policy.revision})
    return policy


def serialize(build, *, cache_hit=False):
    permitted = bool(build.environment_id and build.environment.revision_sha256 in Grant.objects.get(project=build.spec.project).policy["allowed_environment_revision_sha256"])
    return {"id": str(build.id), "name": build.spec.document["name"], "spec_id": str(build.spec_id), "spec_sha256": build.spec.sha256,
            "project_id": str(build.spec.project_id), "connection_id": str(build.spec.connection_id),
            "state": build.state, "manager": build.spec.document["manager"], "cache_hit": cache_hit,
            "job_id": str(build.job_id) if build.job_id else None,
            "attempt_id": str(build.attempt_id) if build.attempt_id else None,
            "environment": {"id": str(build.environment_id), "revision_sha256": build.environment.revision_sha256,
                            "permitted_for_jobs": permitted} if build.environment_id else None,
            "error_code": build.error_code, "receipt": build.receipt,
            "storage": {"estimated_bytes": build.spec.document["estimated_bytes"], "estimated_files": build.spec.document["estimated_files"],
                        "target": "broker_attempt_workdir/environment", "quota_assurance": "advisory; no OS reservation",
                        "measured_bytes": build.receipt.get("receipt", {}).get("measured_bytes"),
                        "measured_files": build.receipt.get("receipt", {}).get("measured_files"),
                        "measured_at": build.receipt.get("receipt", {}).get("measured_at")}}


def create_build(credential, document):
    project = credential.project
    credential = _credential(credential, project)
    policy = _policy(project)
    normalized = normalize_request(document, policy.configuration)
    require_connection(credential, normalized["connection_id"])
    base = core.bound_environment(project, {"connection_id": normalized["connection_id"], "environment": normalized["base_environment"]})
    _bootstrap(base)
    if str(base.id) not in policy.configuration["allowed_base_environment_ids"]:
        raise BrokerError("ENVIRONMENT_BASE_DENIED", "Bootstrap environment is outside build policy.", 403)
    connection = project.connections.get(id=normalized["connection_id"], enabled=True)
    normalized["connection_revision_sha256"] = connection.revision_sha256
    for row in normalized["requirements"]:
        if not Blob.objects.filter(project=project, sha256=row["sha256"], size_bytes=row["size_bytes"]).exists():
            raise BrokerError("OBJECT_MISSING", "Upload every pinned wheel before creating a build.", 404)
    digest = core.digest(normalized)
    with transaction.atomic():
        Project.objects.select_for_update().get(id=project.id)
        spec, _ = EnvironmentSpec.objects.get_or_create(project=project, sha256=digest,
            defaults={"connection": connection, "base_environment": base, "document": normalized})
        cached = spec.builds.filter(state="ready", policy=policy, policy_revision=policy.revision,
                                   policy_sha256=core.digest(policy.configuration), environment__isnull=False).first()
        if cached and project.environments.filter(id=cached.environment_id, connection=connection).exists():
            return serialize(cached, cache_hit=True), 200
        build = EnvironmentBuild.objects.create(spec=spec, policy=policy, policy_revision=policy.revision,
            policy_sha256=core.digest(policy.configuration), credential=credential)
        core.audit(project, credential.id, "environment.build.created", build.id, {"spec_sha256": digest})
    return serialize(build), 201


def prepare_build(build, credential):
    credential = _credential(credential, build.spec.project)
    require_connection(credential, build.spec.connection_id)
    policy = _current_build_policy(build)
    if build.job_id:
        core.prepare_job(build.job)
        EnvironmentBuild.objects.filter(id=build.id, state="draft").update(state="prepared")
        build.refresh_from_db()
        return serialize(build), 200
    document = build.spec.document
    generated = render_build_files(document, build_id=build.id, spec_sha256=build.spec.sha256, policy=policy.configuration)
    # Upload reservations must commit outside the job/idempotency transaction.
    descriptors = []
    for path, content in generated.items():
        blob = store_blob(build.spec.project, content)
        descriptors.append({"path": path, "sha256": blob.sha256, "size_bytes": blob.size_bytes, "executable": False})
    descriptors += [{"path": "_environment/wheels/" + row["filename"], "sha256": row["sha256"],
                     "size_bytes": row["size_bytes"], "executable": False} for row in document["requirements"]]
    request = {"schema_version": "1.0", "project_id": str(build.spec.project_id), "connection_id": str(build.spec.connection_id),
        "name": "env-" + document["name"][:76], "environment": document["base_environment"],
        "command": {"interpreter": "python3", "script": "_environment/build.py", "arguments": []},
        "resources": document["resources"], "slurm": document["slurm"], "files": descriptors,
        "outputs": ["_environment/receipt.json"]}
    with transaction.atomic():
        Project.objects.select_for_update().get(id=build.spec.project_id)
        current = EnvironmentBuild.objects.select_for_update().get(id=build.id)
        _current_build_policy(current)
        if not current.job_id:
            result, _ = core.create_job(credential, request)
            job = Job.objects.get(id=result["id"])
            job.extension_name = "environments"
            job.save(update_fields=["extension_name"])
            current.job = job
            current.save(update_fields=["job"])
    current.refresh_from_db()
    core.prepare_job(current.job)
    EnvironmentBuild.objects.filter(id=current.id, state="draft").update(state="prepared")
    current.refresh_from_db()
    return serialize(current), 200


def validate_dispatch(job, credential, attempt=None):
    build = EnvironmentBuild.objects.select_related("spec__project", "spec__connection").get(job=job)
    _credential(credential, build.spec.project, execute=True)
    _current_build_policy(build)
    if build.attempt_id and attempt is not None and build.attempt_id != attempt.id:
        raise BrokerError("ENVIRONMENT_ATTEMPT_CHANGED", "Build is bound to another attempt.", 409)


def validate_admission(job, credential):
    validate_dispatch(job, credential)
    if Attempt.objects.filter(job=job).exists():
        raise BrokerError("ENVIRONMENT_BUILD_ALREADY_EXECUTED", "A build uses one unique prefix; retry with a fresh build.", 409)


def note_build_attempt(attempt):
    build = EnvironmentBuild.objects.select_for_update().get(job=attempt.job)
    if build.attempt_id not in (None, attempt.id):
        raise BrokerError("ENVIRONMENT_ATTEMPT_CHANGED", "Build already has another attempt.", 409)
    build.attempt = attempt
    build.state = attempt.state
    build.save(update_fields=["attempt", "state"])


def request_build(build, credential):
    if not build.job_id or not build.job.manifest:
        raise BrokerError("NOT_PREPARED", "Prepare the build before requesting execution.", 409)
    with transaction.atomic():
        Project.objects.select_for_update().get(id=build.spec.project_id)
        validate_admission(build.job, credential)
        response = core.request_execution(build.job, credential)
        if response[0].get("attempt_id"):
            note_build_attempt(Attempt.objects.get(id=response[0]["attempt_id"]))
        else:
            build.state = "awaiting_approval"
            build.save(update_fields=["state"])
        return response


def worker_lease_context(attempt):
    build = EnvironmentBuild.objects.filter(job=attempt.job).first()
    if build is None:
        return {}
    return {"build_id": str(build.id), "spec_sha256": build.spec.sha256,
            "attempt_id": str(attempt.id), "generation": attempt.generation}


def _verify_finalization(build, attempt, finalization):
    if not isinstance(finalization, dict) or finalization.get("verified") is not True or type(finalization.get("generation")) is not int or finalization["generation"] != attempt.generation:
        raise ValueError("Unverified or stale finalization")
    identity = {"build_id": str(build.id), "spec_sha256": build.spec.sha256, "attempt_id": str(attempt.id)}
    if any(finalization.get(key) != value for key, value in identity.items()):
        raise ValueError("Finalization identity mismatch")
    workdir = (attempt.native_identity or {}).get("work_dir", "")
    if not workdir.startswith("/") or posixpath.normpath(workdir) != workdir or workdir.split("/")[-1] != str(attempt.id):
        raise ValueError("Invalid native work directory")
    if finalization.get("prefix") != workdir + "/environment":
        raise ValueError("Final environment prefix mismatch")
    remote_root = attempt.connection.configuration.get("remote_root")
    if remote_root and workdir != posixpath.join(remote_root, str(build.spec.project_id), str(build.job_id), str(attempt.id)):
        raise ValueError("Native directory differs from the configured broker root")
    receipt = finalization.get("receipt")
    if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in identity.items()):
        raise ValueError("Build receipt identity mismatch")
    required = {"schema_version": "1.0", "manager": "venv", "prefix_relative": "environment",
        "interpreter_relative": "environment/bin/python", "validated": True,
        "platform_tag": build.spec.document["platform_tag"], "python_major_minor": build.spec.document["python_major_minor"],
        "requirements_sha256": hashlib.sha256(requirements_text(build.spec.document).encode()).hexdigest()}
    if any(receipt.get(key) != value for key, value in required.items()):
        raise ValueError("Build validation receipt does not match the approved recipe")
    for field, maximum in (("measured_bytes", build.policy.configuration["max_environment_bytes"]),
                           ("measured_files", build.policy.configuration["max_environment_files"])):
        value = receipt.get(field)
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError("Build receipt exceeds storage limits")
    if not isinstance(receipt.get("python_version"), str) or not receipt["python_version"].startswith(receipt["python_major_minor"] + "."):
        raise ValueError("Unexpected created interpreter version")
    if not isinstance(receipt.get("measured_at"), str) or datetime.fromisoformat(receipt["measured_at"]).utcoffset() is None:
        raise ValueError("Build measurements require an explicit timestamp")
    return receipt


def sync_attempt(attempt, finalization=None):
    """No remote I/O. Safe after core settlement; failure never rolls accounting back."""
    with transaction.atomic():
        build = EnvironmentBuild.objects.select_for_update(of=("self",)).select_related("spec__project", "spec__connection", "policy").filter(job=attempt.job).first()
        if build is None:
            return
        if build.environment_id:
            return  # Published revisions and their receipts are immutable.
        if build.attempt_id not in (None, attempt.id):
            build.error_code = "ENVIRONMENT_ATTEMPT_CHANGED"
        else:
            build.attempt = attempt
            build.state = attempt.state
            if attempt.state in {"failed", "cancelled", "expired"}:
                build.error_code = "ENVIRONMENT_BUILD_FAILED"
            elif attempt.state == "succeeded":
                if attempt.connection.backend == "fake":
                    build.state, build.error_code = "simulation_only", "SIMULATED_BUILD_NOT_PUBLISHED"
                elif attempt.accounting_state != "settled":
                    build.state = "awaiting_accounting"
                elif finalization is None:
                    build.state = "awaiting_finalization"
                else:
                    try:
                        _current_build_policy(build)
                        receipt = _verify_finalization(build, attempt, finalization)
                    except (ValueError, BrokerError, KeyError, TypeError):
                        build.state, build.error_code = "finalization_failed", "ENVIRONMENT_FINALIZATION_INVALID"
                    else:
                        prefix = finalization["prefix"]
                        configuration = {"kind": "venv", "prefix": prefix, "interpreter": prefix + "/bin/python", "modules": []}
                        revision = core.digest({"configuration": configuration, "spec_sha256": build.spec.sha256,
                                                "build_id": str(build.id), "receipt": receipt})
                        build.environment = Environment.objects.create(owner=build.spec.project.owner, connection=build.spec.connection,
                            name=build.spec.document["name"], configuration=configuration, revision_sha256=revision)
                        build.spec.project.environments.add(build.environment)
                        build.receipt = {**finalization, "receipt": receipt}
                        build.state, build.error_code = "ready", ""
                        core.audit(build.spec.project, "control", "environment.build.ready", build.id, {"environment_id": str(build.environment_id)})
        build.save()


def after_report(attempt, report):
    finalization = report.get("environment_finalization")
    if finalization is not None:
        finalization = {**finalization, "generation": report.get("generation")}
    sync_attempt(attempt, finalization)


after_admission = note_build_attempt


def sync_from_report(worker_report):
    """Recover a durable post-commit hook without re-running any remote operation."""
    data = worker_report.extension_data.get("environments")
    if isinstance(data, dict):
        after_report(worker_report.attempt, {**data, "generation": worker_report.generation})


def promote(build, user):
    project = build.spec.project
    human_permission(user, project, "project_admin")
    if build.state != "ready" or not build.environment_id:
        raise BrokerError("ENVIRONMENT_NOT_READY", "Only a verified real build can be permitted for jobs.", 409)
    with transaction.atomic():
        Project.objects.select_for_update().get(id=project.id)
        grant = Grant.objects.select_for_update().get(project=project)
        allowed = grant.policy["allowed_environment_revision_sha256"]
        if build.environment.revision_sha256 not in allowed:
            grant.policy = {**grant.policy, "revision": grant.policy["revision"] + 1,
                            "allowed_environment_revision_sha256": [*allowed, build.environment.revision_sha256]}
            validate("project-policy.schema.json", grant.policy)
            grant.save(update_fields=["policy"])
            core.audit(project, user.id, "environment.build.promoted", build.id, {"revision_sha256": build.environment.revision_sha256})
    return serialize(build)
