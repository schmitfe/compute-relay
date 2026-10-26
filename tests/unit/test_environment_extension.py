"""Optional scheduled environment builds; unit tests never run an installer."""

import copy
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import sysconfig
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.apps import apps
from django.db import transaction
from django.test import Client
from django.utils import timezone

from compute_relay.control import services as core
from compute_relay.control.auth import token_hash
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Attempt, Connection, Environment, Grant, Job, Project, ProjectCredential, User, WorkerIdentity, WorkerReport
from compute_relay.extensions.environments import rendering
from compute_relay.extensions.environments.remote import REMOTE_SOURCE


def policy(base):
    return {"enabled": True, "allowed_base_environment_ids": [str(base)], "allowed_managers": ["venv"],
        "allowed_sources": ["uploaded_wheels"], "resource_limits": {"cpus": 1, "memory_mib": 1024, "time_limit_seconds": 600},
        "platform_tag": sysconfig.get_platform(), "python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "max_environment_bytes": 1000000000, "max_environment_files": 20000,
        "minimum_free_bytes": 0, "minimum_free_files": 0}


def request(connection, base):
    return {"name": "stdlib-analysis", "connection_id": str(connection),
        "base_environment": {"id": str(base), "revision_sha256": "e" * 64}, "manager": "venv", "requirements": [],
        "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 512, "time_limit_seconds": 60},
        "slurm": {"partition": "debug", "account": "broker"}, "estimated_bytes": 10000000, "estimated_files": 100}


def test_empty_environment_renderer_is_pure_and_has_no_unbounded_install_path(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("renderer executed command"))
    base, connection = uuid4(), uuid4()
    document = rendering.normalize_request(request(connection, base), policy(base))
    files = rendering.render_build_files(document, build_id=uuid4(), spec_sha256="f" * 64, policy=policy(base))
    assert rendering.requirements_text(document) == ""
    compile(files["_environment/build.py"], "build.py", "exec")
    assert b"symlinks=False" in files["_environment/build.py"]
    assert b"--no-index" in files["_environment/build.py"]
    assert b"--require-hashes" in files["_environment/build.py"]
    assert b"prefix=root/'environment'" in files["_environment/build.py"]
    assert b"prefix.rename" not in files["_environment/build.py"]


@pytest.mark.parametrize("manager", ["conda", "micromamba", "poetry", "pip-shell"])
def test_unimplemented_managers_are_rejected_instead_of_being_silently_substituted(manager):
    document = request(uuid4(), uuid4())
    document["manager"] = manager
    with pytest.raises(ValueError, match="Unsupported environment manager"):
        rendering.normalize_request(document, policy(document["base_environment"]["id"]))


def test_pinned_wheel_names_versions_and_hashes_are_bound():
    document = request(uuid4(), uuid4())
    wheel = {"name": "example_pkg", "version": "1.2.3", "filename": "example_pkg-1.2.3-py3-none-any.whl",
             "sha256": "a" * 64, "size_bytes": 100}
    document["requirements"] = [wheel]
    normalized = rendering.normalize_request(document, policy(document["base_environment"]["id"]))
    assert rendering.requirements_text(normalized) == "example-pkg==1.2.3 --hash=sha256:" + "a" * 64 + "\n"
    for changes in ({"version": ">=1.2"}, {"sha256": ""}, {"filename": "../bad.whl"}, {"filename": "other-1.2.3-py3-none-any.whl"}):
        broken = {**document, "requirements": [{**wheel, **changes}]}
        with pytest.raises(ValueError):
            rendering.normalize_request(broken, policy(document["base_environment"]["id"]))


@pytest.fixture
def context(db, settings, tmp_path):
    if not apps.is_installed("compute_relay.extensions.environments"):
        pytest.skip("Run with BROKER_EXTENSIONS=environments to test the optional app")
    from compute_relay.extensions.environments import services
    settings.ARTIFACT_ROOT = tmp_path / "objects"
    owner = User.objects.create_user(username="environment-owner", password=None)
    outsider = User.objects.create_user(username="environment-outsider", password=None)
    worker = WorkerIdentity.objects.create(owner=owner, name="environment-worker", token_hash=token_hash("environment-worker"),
        supported_extensions=["environments"])
    connection = Connection.objects.create(owner=owner, worker=worker, name="synthetic-native", backend="slurm",
        configuration={"remote_root": "/managed/jobs", "site_profile": {"partitions": ["debug"], "accounts": ["broker"], "qos": [], "automatic_execution_allowed": True}}, revision_sha256="c" * 64)
    base = Environment.objects.create(owner=owner, connection=connection, name="bootstrap", configuration={"interpreter": "/usr/bin/python3"}, revision_sha256="e" * 64)
    project = Project.objects.create(owner=owner, name="environment-project")
    project.connections.add(connection)
    project.environments.add(base)
    key = ProjectCredential.objects.create(project=project, issued_by=project.owner, name="builder", prefix="builder", token_hash=token_hash("environment-agent"),
        scopes=["environments:build", "jobs:write", "jobs:execute", "jobs:read", "objects:write"],
        expires_at=timezone.now() + timedelta(days=1))
    grant = Grant.objects.create(project=project, policy={"schema_version": "1.0", "project_id": str(project.id), "revision": 1,
        "mode": "budgeted", "allowed_connection_ids": [str(connection.id)], "allowed_environment_revision_sha256": [base.revision_sha256],
        "allowed_gpu_types": [], "per_job_limits": {"cpu_cores": 1, "gpu_devices": 0, "memory_mib": 1024,
            "time_limit_seconds": 600}, "max_active_jobs": 10, "max_total_jobs": 10,
        "allowance": {"cpu_core_seconds": 10000, "gpu_device_seconds": 0}, "admission_expires_at": (timezone.now()+timedelta(days=1)).isoformat()})
    build_policy = services.update_policy(project, owner, policy(base.id))
    return SimpleNamespace(owner=owner, outsider=outsider, worker=worker, connection=connection, base=base,
        project=project, key=key, grant=grant, policy=build_policy, document=request(connection.id, base.id), services=services)


def prepared(context):
    from compute_relay.extensions.environments.models import EnvironmentBuild
    created, _ = context.services.create_build(context.key, context.document)
    build = EnvironmentBuild.objects.get(id=created["id"])
    context.services.prepare_build(build, context.key)
    build.refresh_from_db()
    return build


def executed(context):
    build = prepared(context)
    response, _ = context.services.request_build(build, context.key)
    build.refresh_from_db()
    return build, Attempt.objects.get(id=response["attempt_id"])


def finalization(build, attempt):
    workdir = f"/managed/jobs/{build.spec.project_id}/{build.job_id}/{attempt.id}"
    attempt.native_identity = {"attempt_id": str(attempt.id), "job_id": "17", "cluster": "synthetic", "work_dir": workdir}
    receipt = {"schema_version": "1.0", "build_id": str(build.id), "spec_sha256": build.spec.sha256,
        "attempt_id": str(attempt.id), "manager": "venv", "prefix_relative": "environment", "interpreter_relative": "environment/bin/python",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.1", "python_major_minor": build.spec.document["python_major_minor"],
        "platform_tag": build.spec.document["platform_tag"], "validated": True, "measured_bytes": 100000,
        "measured_files": 50, "measured_at": timezone.now().isoformat(), "requirements_sha256": hashlib.sha256(b"").hexdigest()}
    return {"verified": True, "build_id": str(build.id), "spec_sha256": build.spec.sha256,
        "attempt_id": str(attempt.id), "prefix": workdir + "/environment", "receipt": receipt, "generation": attempt.generation}


def test_create_prepare_route_uses_the_ordinary_immutable_job(context):
    build = prepared(context)
    assert build.job.extension_name == "environments"
    assert build.job.manifest["extension_name"] == "environments"
    assert build.job.request["command"]["script"] == "_environment/build.py"
    assert build.job.request["environment"]["id"] == str(context.base.id)
    assert not Attempt.objects.exists()
    assert context.services.worker_lease_context(SimpleNamespace(job=build.job, id=uuid4(), generation=3))["spec_sha256"] == build.spec.sha256


def test_environment_admission_consumes_compute_and_lifetime_slots(context):
    build, attempt = executed(context)
    context.grant.refresh_from_db()
    assert context.grant.reserved_cpu == 60 and core.allowance(context.project)["jobs"]["admitted"] == 1
    assert build.attempt_id == attempt.id
    with pytest.raises(BrokerError, match="fresh build"):
        context.services.request_build(build, context.key)
    assert Attempt.objects.count() == 1
    # Replaying preparation never regresses admitted execution status.
    context.services.prepare_build(build, context.key)
    build.refresh_from_db()
    assert build.state == "authorized"


def test_confirmation_and_denial_follow_core_decisions(context):
    context.grant.policy["mode"] = "approval_required"
    context.grant.save()
    build = prepared(context)
    result, _ = context.services.request_build(build, context.key)
    assert result["state"] == "awaiting_approval" and result["attempt_id"] is None
    assert not Attempt.objects.exists()
    with transaction.atomic():
        core.decide(build.job, context.owner, {"execution_request_id": result["id"], "manifest_sha256": build.job.manifest_sha256,
                                            "approved": False, "comment": "Use a smaller dependency set."})
    assert not Attempt.objects.exists()


@pytest.mark.parametrize("restriction", ["scope", "disabled", "policy_generation", "budget", "total_jobs"])
def test_policy_scope_budget_and_lifetime_limits_are_not_bypassed(context, restriction):
    build = prepared(context)
    if restriction == "scope":
        context.key.scopes.remove("environments:build");context.key.save()
    elif restriction == "disabled":
        context.policy.configuration["enabled"] = False;context.policy.save()
    elif restriction == "policy_generation":
        context.services.update_policy(context.project, context.owner, context.policy.configuration)
    elif restriction == "budget":
        context.grant.policy["allowance"]["cpu_core_seconds"] = 1;context.grant.save()
    else:
        context.grant.policy["max_total_jobs"] = 0;context.grant.save()
    with pytest.raises(BrokerError):
        context.services.request_build(build, context.key)
    assert not Attempt.objects.exists()


def test_real_verified_settled_build_publishes_without_broadening_project_grant(context):
    build, attempt = executed(context)
    metadata = finalization(build, attempt)
    attempt.state, attempt.accounting_state = "succeeded", "settled"
    attempt.save()
    context.services.sync_attempt(attempt, metadata)
    build.refresh_from_db();context.grant.refresh_from_db()
    assert build.state == "ready" and build.environment.connection_id == context.connection.id
    assert build.environment.configuration["prefix"] == metadata["prefix"]
    assert build.environment.revision_sha256 not in context.grant.policy["allowed_environment_revision_sha256"]
    cached, status = context.services.create_build(context.key, context.document)
    assert status == 200 and cached["cache_hit"] is True and cached["id"] == str(build.id)
    assert cached["environment"]["permitted_for_jobs"] is False
    with pytest.raises(BrokerError):
        context.services.promote(build, context.outsider)
    promoted = context.services.promote(build, context.owner)
    assert promoted["environment"]["permitted_for_jobs"] is True
    before = build.environment_id
    context.services.sync_attempt(attempt, metadata)
    build.refresh_from_db()
    assert build.environment_id == before


def test_async_worker_environment_receipt_publishes_via_durable_core_report(context, tmp_path, django_capture_on_commit_callbacks):
    from compute_relay.worker.async_runner import AsyncWorker
    from compute_relay.worker.journal import Journal
    from test_async_worker import Backend, Control
    build, attempt = executed(context)
    metadata = finalization(build, attempt)
    attempt.dispatch_started = True
    attempt.state = "queued"
    attempt.save()
    lease = core.lease_work(context.worker)["lease"]
    assert lease["operation"] == "observe"
    assert lease["extension_context"]["environments"]["build_id"] == str(build.id)
    backend, control = Backend(), Control()
    backend.connection = {**context.connection.configuration, "backend": "slurm", "revision_sha256": context.connection.revision_sha256}
    helper_calls = []
    async def helper(source, request):
        helper_calls.append((source, request))
        await asyncio.sleep(0)
        return metadata
    backend.transport = SimpleNamespace(call_helper=helper)
    worker = AsyncWorker(control, {str(context.connection.id): backend.connection}, Journal(tmp_path / "journal"),
        owner_id=context.owner.id, backend_factory=lambda configuration: backend, extensions=["environments"])
    asyncio.run(worker.execute(lease))
    payload = control.calls[-1][2]
    assert payload["extension_data"]["environments"]["generation"] == lease["generation"]
    assert helper_calls[0][0] == REMOTE_SOURCE
    assert helper_calls[0][1]["native_identity"] == attempt.native_identity
    with django_capture_on_commit_callbacks(execute=True):
        response = core.report(context.worker, attempt.id, payload)
    assert response["accepted"] is True
    build.refresh_from_db();attempt.refresh_from_db();context.grant.refresh_from_db()
    assert build.state == "ready" and attempt.accounting_state == "settled"
    assert build.environment.configuration["prefix"] == metadata["prefix"]
    assert context.grant.spent_cpu == 2 and context.grant.reserved_cpu == 0
    assert build.environment.revision_sha256 not in context.grant.policy["allowed_environment_revision_sha256"]


@pytest.mark.parametrize("case", ["fake", "failed", "unsettled", "stale", "wrong_spec", "wrong_prefix", "wrong_platform", "policy_changed"])
def test_invalid_unknown_or_simulated_builds_never_publish(context, case):
    build, attempt = executed(context)
    metadata = finalization(build, attempt)
    attempt.state, attempt.accounting_state = "succeeded", "settled"
    if case == "fake":
        context.connection.backend = "fake";context.connection.save();attempt.connection = context.connection
    elif case == "failed": attempt.state = "failed"
    elif case == "unsettled": attempt.accounting_state = "settlement_pending"
    elif case == "stale": metadata["generation"] += 1
    elif case == "wrong_spec": metadata["spec_sha256"] = "a" * 64
    elif case == "wrong_prefix": metadata["prefix"] = "/somewhere/else"
    elif case == "wrong_platform": metadata["receipt"]["platform_tag"] = "other-platform"
    else: context.services.update_policy(context.project, context.owner, context.policy.configuration)
    attempt.save()
    context.services.sync_attempt(attempt, metadata)
    build.refresh_from_db()
    assert build.environment_id is None and build.state != "ready"
    assert Attempt.objects.count() == 1


def test_extension_api_scope_project_isolation_and_human_policy_authority(context):
    client = Client()
    path = f"/api/v1/projects/{context.project.id}/environment-builds"
    response = client.post(path, data=json.dumps(context.document), content_type="application/json",
        HTTP_AUTHORIZATION="Bearer environment-agent", HTTP_IDEMPOTENCY_KEY="build-once")
    assert response.status_code == 201, response.content
    replay = client.post(path, data=json.dumps(context.document), content_type="application/json",
        HTTP_AUTHORIZATION="Bearer environment-agent", HTTP_IDEMPOTENCY_KEY="build-once")
    assert replay.json()["id"] == response.json()["id"]
    rejected = client.put(f"/api/v1/projects/{context.project.id}/environment-build-policy", data=json.dumps(context.policy.configuration),
        content_type="application/json", HTTP_AUTHORIZATION="Bearer environment-agent")
    assert rejected.status_code in {403, 400}
    denied = client.get(f"/api/v1/projects/{uuid4()}/environment-builds", HTTP_AUTHORIZATION="Bearer environment-agent")
    assert denied.status_code == 404


def test_human_policy_page_uses_project_scoped_controls_instead_of_raw_json(context):
    client = Client();client.force_login(context.owner)
    path = f"/projects/{context.project.id}/environment-build-policy/"
    response = client.get(path)
    assert response.status_code == 200 and b"Approved starting environments" in response.content
    assert b"<textarea" not in response.content
    fields = {"enabled": "on", "allowed_bases": [str(context.base.id)], "cpus": 1, "memory_mib": 1024,
        "time_limit_minutes": 5, "platform_tag": sysconfig.get_platform(), "python_major_minor": context.policy.configuration["python_major_minor"],
        "max_environment_bytes": 1000000000, "max_environment_files": 20000, "minimum_free_bytes": 1000000, "minimum_free_files": 100}
    response = client.post(path, fields)
    assert response.status_code == 302
    context.policy.refresh_from_db()
    assert context.policy.configuration["resource_limits"]["time_limit_seconds"] == 300
    unrelated = Environment.objects.create(owner=context.outsider, connection=context.connection, name="unassigned",
        configuration={"interpreter": "/usr/bin/python3"}, revision_sha256="f" * 64)
    response = client.post(path, {**fields, "allowed_bases": [str(unrelated.id)]})
    assert response.status_code == 200 and b"Select a valid choice" in response.content


def test_generic_job_execution_route_does_not_bypass_environment_build_authority(context):
    build = prepared(context)
    context.key.scopes.remove("environments:build")
    context.key.save()
    client = Client()
    response = client.post(f"/api/v1/jobs/{build.job_id}/execution-requests", data="{}", content_type="application/json",
        HTTP_AUTHORIZATION="Bearer environment-agent", HTTP_IDEMPOTENCY_KEY="generic-bypass")
    assert response.status_code == 403, response.content
    assert not Attempt.objects.exists()


@pytest.mark.parametrize("change", ["extension", "scope", "policy", "worker"])
def test_current_authority_is_checked_again_before_native_dispatch(context, settings, change):
    build, attempt = executed(context)
    lease = core.lease_work(context.worker)["lease"]
    assert lease["extension_context"]["environments"]["build_id"] == str(build.id)
    if change == "extension":
        settings.BROKER_EXTENSIONS = []
    elif change == "scope":
        context.key.scopes.remove("environments:build");context.key.save()
    elif change == "policy":
        context.services.update_policy(context.project, context.owner, context.policy.configuration)
    else:
        context.worker.supported_extensions = [];context.worker.save()
    with pytest.raises(BrokerError):
        core.dispatch(context.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
            "manifest_sha256": build.job.manifest_sha256, "connection_revision_sha256": context.connection.revision_sha256})
    attempt.refresh_from_db()
    assert attempt.dispatch_started is False


def test_disabling_extension_retains_core_monitoring_and_accounting(context, settings):
    build, attempt = executed(context)
    finalization(build, attempt)
    attempt.dispatch_started, attempt.state = True, "running"
    attempt.save()
    settings.BROKER_EXTENSIONS = []
    lease = core.lease_work(context.worker)["lease"]
    assert lease["operation"] == "observe" and "extension_context" not in lease
    assert lease["attempt_id"] == str(attempt.id)


def test_changed_policy_during_human_review_denies_admission_without_consuming_a_slot(context):
    context.grant.policy["mode"] = "approval_required";context.grant.save()
    build = prepared(context)
    response, _ = context.services.request_build(build, context.key)
    context.services.update_policy(context.project, context.owner, context.policy.configuration)
    with pytest.raises(BrokerError), transaction.atomic():
        core.decide(build.job, context.owner, {"execution_request_id": response["id"], "manifest_sha256": build.job.manifest_sha256,
                                             "approved": True, "comment": "Build this recipe."})
    assert not Attempt.objects.exists()
    context.grant.refresh_from_db()
    assert context.grant.reserved_cpu == build.job.manifest["reservation"]["cpu_core_seconds"]


def test_missing_pinned_wheel_fails_before_preparation_or_execution(context):
    document = copy.deepcopy(context.document)
    document["requirements"] = [{"name": "example", "version": "1.0", "filename": "example-1.0-py3-none-any.whl",
        "sha256": "a" * 64, "size_bytes": 128}]
    with pytest.raises(BrokerError, match="Upload and verify"):
        context.services.create_build(context.key, document)
    assert not Job.objects.exists() and not Attempt.objects.exists()


@pytest.mark.parametrize("kind", ["venv", "poetry", "conda"])
def test_builder_rejects_bootstraps_whose_runtime_initialization_is_not_preserved(context, kind):
    context.base.configuration = {"kind": kind, "interpreter": "/trusted/bin/python3"}
    context.base.save()
    with pytest.raises(BrokerError, match="system or module-provided Python"):
        context.services.update_policy(context.project, context.owner, context.policy.configuration)
    with pytest.raises(BrokerError, match="system or module-provided Python"):
        context.services.create_build(context.key, context.document)
    assert not Job.objects.exists() and not Attempt.objects.exists()


def test_durable_worker_metadata_can_recover_publication_without_remote_io(context, monkeypatch):
    build, attempt = executed(context)
    metadata = finalization(build, attempt)
    attempt.state, attempt.accounting_state = "succeeded", "settled";attempt.save()
    report = WorkerReport.objects.create(lease_id=uuid4(), attempt=attempt, generation=attempt.generation,
        payload_sha256="d" * 64, response={"accepted": True}, extension_data={"environments": {
            "environment_finalization": metadata, "generation": -1}})
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("publication ran a subprocess"))
    context.services.sync_from_report(report)
    build.refresh_from_db()
    assert build.state == "ready" and build.receipt["generation"] == attempt.generation


def test_worker_finalizes_only_metadata_and_fake_results_never_look_verified():
    from compute_relay.extensions.environments import worker
    attempt, build = str(uuid4()), str(uuid4())
    native = {"attempt_id": attempt}
    context = {"attempt_id": attempt, "build_id": build, "spec_sha256": "a" * 64, "generation": 7}
    calls = []
    backend = SimpleNamespace(connection={"backend": "slurm", "remote_root": "/trusted/jobs", "ssh": {"identity_file": "/private/key"}},
        transport=SimpleNamespace(call_helper=lambda source, request: calls.append((source, request)) or {"verified": True}))
    result = worker.after_observation(backend, native, {"extension_name": "environments"}, context)
    assert result == {"environment_finalization": {"verified": True}, "generation": 7}
    assert calls[0][0] == REMOTE_SOURCE and calls[0][1]["config"] == {"remote_root": "/trusted/jobs"}
    backend.connection["backend"] = "fake"
    result = worker.after_observation(backend, native, {"extension_name": "environments"}, context)
    assert result["environment_finalization"]["verified"] is False and len(calls) == 1


def test_optional_mcp_tools_have_no_human_policy_or_shell_authority():
    from compute_relay.mcp.facade import ProjectFacade, create_server
    facade = ProjectFacade(SimpleNamespace(), str(uuid4()), enabled_extensions=["environments"])
    tools = asyncio.run(create_server(facade).list_tools())
    names = {tool.name for tool in tools}
    assert {"get_environment_build_policy", "create_environment_build", "prepare_environment_build", "request_environment_build", "get_environment_build"} <= names
    assert not {"promote_environment", "set_environment_build_policy", "run_shell"} & names


def test_metadata_finalizer_never_executes_the_created_interpreter(tmp_path):
    project, job, attempt, build = map(lambda _: str(uuid4()), range(4))
    root = tmp_path / "cluster"
    directory = root / project / job / attempt
    (directory / "_environment").mkdir(parents=True)
    (directory / "environment/bin").mkdir(parents=True, mode=0o700)
    (directory / "environment").chmod(0o700)
    (root / "_broker_journal").mkdir()
    marker = tmp_path / "interpreter-was-executed"
    python = directory / "environment/bin/python"
    python.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    python.chmod(0o700)
    (directory / "environment/pyvenv.cfg").write_text("home = /trusted/bootstrap\n")
    (directory / "environment/pyvenv.cfg").chmod(0o600)
    identity = {"attempt_id": attempt, "work_dir": str(directory), "job_id": "17"}
    spec = "f" * 64
    recipe = json.dumps({"build_id": build, "spec_sha256": spec}).encode()
    (directory / "_environment/recipe.json").write_bytes(recipe)
    receipt = {"build_id": build, "spec_sha256": spec, "attempt_id": attempt, "validated": True,
               "manager": "venv", "prefix_relative": "environment", "interpreter_relative": "environment/bin/python"}
    (directory / "_environment/receipt.json").write_text(json.dumps(receipt))
    journal = {"outcome": "submitted", "native_identity": identity, "attempt_path": f"{project}/{job}/{attempt}",
        "manifest": {"extension_name": "environments", "job_revision_id": job, "request": {"project_id": project, "command": {"script": "_environment/build.py"},
            "outputs": ["_environment/receipt.json"]}, "files": [{"path": "_environment/recipe.json", "sha256": hashlib.sha256(recipe).hexdigest()}]}}
    (root / "_broker_journal" / (attempt + ".json")).write_text(json.dumps(journal))
    request_data = {"operation": "finalize_environment", "config": {"remote_root": str(root)}, "native_identity": identity,
                    "build_id": build, "spec_sha256": spec}
    result = subprocess.run([sys.executable, "-I", "-c", REMOTE_SOURCE], input=json.dumps(request_data), capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and json.loads(result.stdout)["verified"] is True
    assert not marker.exists()
    python.unlink();python.symlink_to("/usr/bin/python3")
    refused = subprocess.run([sys.executable, "-I", "-c", REMOTE_SOURCE], input=json.dumps(request_data), capture_output=True, text=True, timeout=5)
    assert json.loads(refused.stdout)["verified"] is False
    python.unlink();os.mkfifo(python)
    refused = subprocess.run([sys.executable, "-I", "-c", REMOTE_SOURCE], input=json.dumps(request_data), capture_output=True, text=True, timeout=5)
    assert json.loads(refused.stdout)["verified"] is False


def test_module_python_build_preserves_runtime_setup_when_publishing_and_reusing(context):
    from compute_relay.backends.environments import render_environment
    setup = {"modules": ["gcc/14.1.0", "python3/3.12.4"], "module_init": "", "module_paths": ["/opt/apps/modules/Software"]}
    context.base.configuration = {"kind": "modules", "interpreter": "python3", **setup}
    context.base.save()
    context.services.update_policy(context.project, context.owner, context.policy.configuration)
    build, attempt = executed(context)
    metadata = finalization(build, attempt)
    attempt.state, attempt.accounting_state = "succeeded", "settled"; attempt.save()
    context.services.sync_attempt(attempt, metadata)
    build.refresh_from_db()
    assert build.state == "ready"
    assert all(build.environment.configuration[key] == value for key, value in setup.items())
    wrapper, command = render_environment(build.environment.configuration, "python3")
    assert "module load gcc/14.1.0 python3/3.12.4" in wrapper
    assert command == [metadata["prefix"] + "/bin/python"]
    assert not context.services.serialize(build)["environment"]["permitted_for_jobs"]
    context.services.promote(build, context.owner)
    assert context.services.serialize(build)["environment"]["permitted_for_jobs"]


def test_agent_policy_discovery_includes_approved_module_bootstrap_reference(context):
    context.base.configuration = {"kind": "modules", "modules": ["python3/3.12.4"], "interpreter": "python3"}
    context.base.save()
    context.services.update_policy(context.project, context.owner, context.policy.configuration)
    response = Client().get(f"/api/v1/projects/{context.project.id}/environment-build-policy",
        HTTP_AUTHORIZATION="Bearer environment-agent")
    assert response.status_code == 200
    assert response.json()["bootstrap_environments"] == [{"id": str(context.base.id), "revision_sha256": context.base.revision_sha256,
        "name": context.base.name, "connection_id": str(context.connection.id), "configuration": context.base.configuration}]
    context.key.allowed_connection_ids = []; context.key.save()
    assert Client().get(f"/api/v1/projects/{context.project.id}/environment-build-policy",
        HTTP_AUTHORIZATION="Bearer environment-agent").json()["bootstrap_environments"] == []
