from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from compute_broker.control.auth import token_hash
from compute_broker.control.models import Connection, Project, ProjectCredential, User, WorkerIdentity


@pytest.fixture
def resources(db):
    owner = User.objects.create_user(username="storage-owner")
    outsider = User.objects.create_user(username="storage-outsider")
    profile = {"alias": "protected", "backend": "fake", "revision_sha256": "c" * 64,
               "allowed_hosts": ["storage.invalid"], "allowed_users": ["researcher"], "allowed_ports": [22],
               "remote_root_prefixes": ["/approved/project-space"], "site_profile": {}}
    worker = WorkerIdentity.objects.create(owner=owner, name="protected storage worker", token_hash=token_hash("storage-worker"), profiles=[profile])
    configuration = {"backend": "fake", "credential_profile": "protected", "credential_profile_revision_sha256": "c" * 64,
                     "ssh": {"host": "storage.invalid", "user": "researcher", "port": 22},
                     "remote_root": "/approved/project-space/jobs", "profile_revision": 1, "site_profile": {}}
    connection = Connection.objects.create(owner=owner, worker=worker, name="Storage cluster", backend="fake",
                                           configuration=configuration, revision_sha256="a" * 64)
    project = Project.objects.create(owner=owner, name="Storage project")
    project.connections.add(connection)
    secret = "storage-project-token"
    credential = ProjectCredential.objects.create(project=project, issued_by=project.owner, name="planner", prefix="planner", token_hash=token_hash(secret),
        scopes=["jobs:read"], expires_at=timezone.now() + timedelta(days=1))
    return SimpleNamespace(owner=owner, outsider=outsider, profile=profile, worker=worker, connection=connection,
                           project=project, credential=credential, token=secret)
