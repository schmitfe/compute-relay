import sys
import sysconfig
from datetime import timedelta
from types import SimpleNamespace
import pytest
from django.apps import apps
from django.utils import timezone
from compute_relay.control.auth import token_hash
from compute_relay.control.models import Connection, Environment, Grant, Project, ProjectCredential, User, WorkerIdentity

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
