"""Tiny human-requested diagnostics through ordinary job review and accounting."""

from datetime import datetime, timedelta
from uuid import uuid4

from django import forms
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from compute_broker.backends.preparation import BackendError
from compute_broker.backends.environments import validate_environment_config, render_environment, EnvironmentProfileError
from compute_broker.domain.cluster_configuration import validate_cluster_configuration, ClusterConfigurationError, TIMING_FIELDS
from . import services
from .auth import new_token, token_hash, require_credential_actor
from .errors import BrokerError
from .identity.authority import cached_user_active
from .identity.models import AuthenticationIdentity
from .management import human_view
from .models import (ClusterCheckConfiguration, Connection, Environment, Grant, Job, Project,
    ProjectCredential, User, WorkerIdentity)
from .storage import read_blob, store_blob


CPU_ALLOWANCE = 3600
TOTAL_JOBS = 20
SOURCE = b'''import json
import socket
import sys
import time
from pathlib import Path

started = time.monotonic()
time.sleep(2)
result = {"marker": "compute-broker-cluster-check-v1", "hostname": socket.gethostname(),
          "python": sys.version, "elapsed_seconds": round(time.monotonic() - started, 3)}
text = json.dumps(result, sort_keys=True) + "\\n"
Path("cluster-check.json").write_text(text, encoding="utf-8")
print(text, end="", flush=True)
'''
SCOPES = ["objects:write", "jobs:write", "jobs:execute", "jobs:read", "jobs:cancel", "artifacts:read"]


def _owned_connection(user, connection_id):
    current = User.objects.filter(id=user.id, is_active=True).first()
    if current is None or not cached_user_active(current):
        raise BrokerError("AUTH_REQUIRED", "Current account verification is required.", 403)
    return get_object_or_404(Connection, id=connection_id, owner=current, archived=False)


def resource_plan(connection, policy=None):
    if (not connection.enabled or not connection.setup_complete or not connection.configuration_confirmed
            or connection.worker.revoked or connection.worker.owner_id != connection.owner_id):
        raise BrokerError("CLUSTER_NOT_READY", "Finish and confirm this cluster's configuration with your current worker before testing.", 409)
    try:
        profile = validate_cluster_configuration(connection.configuration.get("site_profile", {}))
    except ClusterConfigurationError:
        raise BrokerError("CHECK_CONFIGURATION_REQUIRED", "Configure finite cluster resource limits before preparing a test.", 409) from None
    if any(type(profile.get(field)) is not int for field in TIMING_FIELDS):
        raise BrokerError("CHECK_TIMING_REQUIRED", "Enter explicit finite termination, overtime and cleanup bounds in Cluster configuration before testing.", 409)
    if profile.get("allocated_gpus", 0):
        raise BrokerError("CHECK_GPU_ROUNDING", "This connection's allocation rounding cannot provide a CPU-only diagnostic. Use an appropriate CPU connection.", 409)
    limits = policy.get("per_job_limits", {}) if policy else {}
    cpus = min(2, profile["maximum_cpus"], limits.get("cpu_cores", 1024))
    effective = max(cpus, profile.get("allocated_cpus", cpus))
    duration = 60 + sum(profile[field] for field in TIMING_FIELDS)
    if (cpus < 1 or effective > min(profile["maximum_cpus"], limits.get("cpu_cores", 1024))
            or min(profile["maximum_memory_mib"], limits.get("memory_mib", 16777216)) < 128
            or profile["maximum_time_limit_seconds"] < 60
            or duration > limits.get("maximum_charge_duration_seconds", 1209600)):
        raise BrokerError("CHECK_RESOURCE_LIMIT", "The tiny test does not fit the current cluster or diagnostic project envelope.", 409)
    return {"resources": {"nodes": 1, "cpus": cpus, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
        "effective_cpus": effective, "maximum_charge_duration_seconds": duration,
        "reservation_cpu_seconds": effective * duration, "profile": profile}


class ClusterCheckForm(forms.Form):
    partition = forms.ChoiceField(label="Partition")
    account = forms.ChoiceField(required=False, label="Account")
    qos = forms.ChoiceField(required=False, label="Job QoS")
    environment = forms.ModelChoiceField(queryset=Environment.objects.none(), required=False, label="Python environment")
    submission_id = forms.UUIDField(widget=forms.HiddenInput)
    connection_revision = forms.RegexField(r"^[0-9a-f]{64}$", widget=forms.HiddenInput)

    def __init__(self, *args, user, connection, configuration=None, **kwargs):
        super().__init__(*args, **kwargs)
        profile = connection.configuration.get("site_profile", {})
        if not isinstance(profile, dict):
            profile = {}
        for field, key in (("partition", "partitions"), ("account", "accounts"), ("qos", "qos")):
            names = profile.get(key, [])
            if not isinstance(names, list):
                names = []
            self.fields[field].choices = [(name, name) for name in names]
            self.fields[field].required = bool(names) or field == "partition"
            if names:
                self.fields[field].initial = names[0]
            elif field != "partition":
                self.fields[field].choices = [("", "Cluster default (no explicit override)")]
        environments = Environment.objects.filter(owner=user, connection=connection).order_by("name", "id")
        if configuration:
            policy = configuration.project.grant.policy
            environments = environments.filter(project=configuration.project,
                revision_sha256__in=policy.get("allowed_environment_revision_sha256", []))
        self.fields["environment"].queryset = environments
        self.fields["environment"].empty_label = "Use saved diagnostic Python" if configuration else "Use diagnostic system Python"
        self.fields["submission_id"].initial = uuid4()
        self.fields["connection_revision"].initial = connection.revision_sha256

    def clean_environment(self):
        environment = self.cleaned_data["environment"]
        if environment is not None:
            try:
                render_environment(environment.configuration, "python3")
            except EnvironmentProfileError:
                raise forms.ValidationError("Choose a valid Python environment for this cluster.") from None
        return environment


def _configuration(connection):
    return ClusterCheckConfiguration.objects.select_related("project", "environment", "credential").filter(connection=connection).first()


def _check_existing(configuration, connection):
    project, credential = configuration.project, configuration.credential
    if (project.owner_id != connection.owner_id or credential.project_id != project.id
            or credential.issued_by_id != connection.owner_id or configuration.environment.owner_id != connection.owner_id
            or configuration.environment.connection_id != connection.id):
        raise BrokerError("CHECK_BINDING_INVALID", "The diagnostic configuration needs operator repair; a new allowance will not be created.", 409)
    grant = Grant.objects.get(project=project)
    policy = grant.policy
    if project.paused or datetime.fromisoformat(policy["admission_expires_at"].replace("Z", "+00:00")) <= timezone.now():
        raise BrokerError("CHECK_PROJECT_UNAVAILABLE", "The diagnostic project is paused or expired. Review its existing settings; tests never renew or reset its allowance.", 409)
    if (credential.revoked or credential.expires_at <= timezone.now() or not set(SCOPES) <= set(credential.scopes)
            or credential.execution_mode != "approval_required" or policy["mode"] != "approval_required"):
        raise BrokerError("CHECK_CREDENTIAL_UNAVAILABLE", "The existing diagnostic project/key must be current and require human approval. Review its settings; tests do not replace it.", 409)
    require_credential_actor(credential)
    return policy


def _default_environment(connection):
    software = connection.discovery_snapshot.get("software", {})
    candidates = software.get("python", []) if isinstance(software, dict) else []
    for candidate in candidates if isinstance(candidates, list) else []:
        if isinstance(candidate, dict) and candidate.get("kind") == "system":
            profile = {"kind": "system", "interpreter": candidate.get("interpreter"), "modules": []}
            try:
                validate_environment_config(profile)
                return profile
            except EnvironmentProfileError:
                pass
    # This is a reviewed compute-side reference, not a claim it was discovered
    # or tested on the compute node. A missing interpreter produces a job failure.
    return {"kind": "system", "interpreter": "/usr/bin/python3", "modules": []}


def _ensure_configuration(user, connection_id, revision, environment):
    with transaction.atomic():
        current = User.objects.select_for_update(no_key=True).get(id=user.id)
        list(AuthenticationIdentity.objects.select_for_update().filter(user=current))
        connection = _owned_connection(current, connection_id)
        worker = WorkerIdentity.objects.select_for_update().get(id=connection.worker_id)
        existing = _configuration(connection)
        if existing:
            Project.objects.select_for_update().get(id=existing.project_id)
            Grant.objects.select_for_update().get(project_id=existing.project_id)
        connection = get_object_or_404(Connection.objects.select_for_update(), id=connection_id,
            owner=current, worker=worker, archived=False)
        if connection.revision_sha256 != revision:
            raise BrokerError("REVISION_CHANGED", "Cluster configuration changed. Reload the test preview before preparing it.", 409)
        policy = _check_existing(existing, connection) if existing else None
        plan = resource_plan(connection, policy)
        if existing:
            return existing, connection, plan
        if plan["reservation_cpu_seconds"] > CPU_ALLOWANCE:
            raise BrokerError("CHECK_ALLOWANCE_TOO_SMALL", "The conservative reservation exceeds the finite diagnostic allowance. Use a separately reviewed project; the test will not enlarge its budget.", 409)
        if environment is not None:
            environment = get_object_or_404(Environment, id=environment.id, owner=current, connection=connection)
        else:
            config = _default_environment(connection)
            environment = Environment.objects.create(owner=current, connection=connection, name="Cluster test system Python",
                configuration=config, revision_sha256=services.digest(config))
        project = Project.objects.create(owner=current, name=("Cluster tests: " + connection.name)[:100])
        project.connections.add(connection)
        project.environments.add(environment)
        expiry = timezone.now() + timedelta(days=30)
        policy = {"schema_version": "1.0", "project_id": str(project.id), "revision": 1, "mode": "approval_required",
            "allowed_connection_ids": [str(connection.id)], "allowed_environment_revision_sha256": [environment.revision_sha256],
            "allowed_gpu_types": [], "per_job_limits": {"cpu_cores": plan["effective_cpus"], "gpu_devices": 0,
                "memory_mib": 128, "time_limit_seconds": 60, "maximum_charge_duration_seconds": plan["maximum_charge_duration_seconds"]},
            "max_active_jobs": 1, "max_total_jobs": TOTAL_JOBS,
            "allowance": {"cpu_core_seconds": CPU_ALLOWANCE, "gpu_device_seconds": 0}, "admission_expires_at": expiry.isoformat()}
        Grant.objects.create(project=project, policy=policy)
        credential = ProjectCredential.objects.create(project=project, issued_by=current, name="Internal cluster test",
            token_hash=token_hash(new_token()), prefix="internal-check", scopes=SCOPES, expires_at=expiry,
            allowed_connection_ids=[str(connection.id)], execution_mode="approval_required")
        configuration = ClusterCheckConfiguration.objects.create(connection=connection, project=project,
            environment=environment, credential=credential)
        services.audit(project, current.id, "cluster.check_configured", connection.id)
        services.audit(project, current.id, "credential.created", credential.id)
        return configuration, connection, plan


def prepare_check(user, connection_id, data):
    configuration, connection, plan = _ensure_configuration(user, connection_id, data["connection_revision"], data.get("environment"))
    environment = data.get("environment") or configuration.environment
    credential, project = configuration.credential, configuration.project
    # Filesystem writes must not be nested inside the idempotent DB transaction.
    blob = store_blob(project, SOURCE)
    document = {"schema_version": "1.0", "project_id": str(project.id), "connection_id": str(connection.id),
        "name": "cluster-check-" + data["submission_id"].hex[:12],
        "command": {"interpreter": "python3", "script": "cluster-check.py", "arguments": []},
        "environment": {"id": str(environment.id), "revision_sha256": environment.revision_sha256},
        "resources": plan["resources"], "slurm": {key: data[key] for key in ("partition", "account", "qos") if data.get(key)},
        "files": [{"path": "cluster-check.py", "sha256": blob.sha256, "size_bytes": blob.size_bytes, "executable": False}],
        "outputs": ["cluster-check.json"]}
    payload = {"request": document, "connection_revision": data["connection_revision"]}
    def create():
        current = Connection.objects.select_for_update().get(id=connection.id)
        if current.revision_sha256 != data["connection_revision"]:
            raise BrokerError("REVISION_CHANGED", "Cluster configuration changed. Reload the test preview.", 409)
        resource_plan(current)
        require_credential_actor(credential)
        return services.create_job(credential, document)
    created, _ = services.idempotent(project, "cluster-check:create", str(data["submission_id"]), payload,
        create)
    job = Job.objects.get(id=created["id"])
    try:
        services.prepare_job(job)
    except (BackendError, ObjectDoesNotExist):
        raise BrokerError("CHECK_PREPARATION_FAILED", "The test could not be prepared with the current connection and environment. Reload the preview and check their settings.", 409) from None
    job.refresh_from_db()
    if job.manifest["connection_revision_sha256"] != data["connection_revision"]:
        raise BrokerError("REVISION_CHANGED", "Cluster configuration changed during preparation. Reload the test preview.", 409)
    def execute():
        configuration.refresh_from_db()
        _check_existing(configuration, Connection.objects.get(id=connection.id))
        return services.request_execution(job, configuration.credential)
    services.idempotent(project, "cluster-check:execute", str(data["submission_id"]), {"job_id": str(job.id)}, execute)
    return job


def _results(jobs):
    results = []
    for job in jobs:
        attempts = []
        for attempt in job.attempts.order_by("-created_at")[:3]:
            outputs = []
            for artifact in attempt.artifacts.select_related("blob").filter(path="cluster-check.json"):
                text = "Output is too large for an inline preview."
                if artifact.blob.size_bytes <= 65536:
                    try:
                        text = read_blob(artifact.blob).decode("utf-8", errors="replace")
                    except BrokerError:
                        text = "Stored output could not be verified."
                outputs.append({"path": artifact.path, "sha256": artifact.blob.sha256, "text": text})
            attempts.append({"id": attempt.id, "state": attempt.state, "accounting_state": attempt.accounting_state,
                "stdout": attempt.stdout[:8192], "stderr": attempt.stderr[:8192], "outputs": outputs})
        results.append({"job": job, "attempts": attempts})
    return results


@human_view
def check(request, connection_id):
    connection = _owned_connection(request.user, connection_id)
    configuration = _configuration(connection)
    blocked = ""
    plan = None
    try:
        policy = _check_existing(configuration, connection) if configuration else None
        plan = resource_plan(connection, policy)
    except BrokerError as exc:
        if exc.code == "CHECK_BINDING_INVALID":
            raise
        blocked = exc.message
    form = ClusterCheckForm(request.POST if request.method == "POST" else None,
        user=request.user, connection=connection, configuration=configuration)
    if request.method == "POST":
        if blocked:
            raise BrokerError("CHECK_UNAVAILABLE", blocked, 409)
        if form.is_valid():
            job = prepare_check(request.user, connection.id, form.cleaned_data)
            return redirect(f"/review/{job.id}/")
    recent = list(Job.objects.with_summary().filter(project=configuration.project).order_by("-created_at")[:10]) if configuration else []
    return render(request, "broker/cluster_test.html", {"cluster": connection, "form": form, "configuration": configuration,
        "plan": plan, "blocked_reason": blocked, "source": SOURCE.decode(), "recent_jobs": recent, "recent_results": _results(recent),
        "allowance": services.allowance(configuration.project) if configuration else None,
        "initial_cpu_allowance": CPU_ALLOWANCE, "initial_job_limit": TOTAL_JOBS})
