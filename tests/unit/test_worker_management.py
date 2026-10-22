"""Human one-time worker enrollment; no worker process or cluster is contacted."""

from datetime import timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.contrib.sessions.models import Session
from django.test import Client
from django.urls import path
from django.utils import timezone

from compute_broker.control import worker_management
from compute_broker.control.auth import token_hash
from compute_broker.control.models import (AuditEvent, ClusterRegistration, Connection, IdempotencyRecord,
    Project, ProjectCredential, User, WorkerIdentity)


urlpatterns = [
    path("workers/new/", worker_management.worker_new),
    path("workers/<uuid:worker_id>/", worker_management.worker_detail),
]


@pytest.fixture
def enrollment(db, settings):
    settings.ROOT_URLCONF = __name__
    owner = User.objects.create_user(username="worker-enrollment-owner", password=None)
    outsider = User.objects.create_user(username="worker-enrollment-outsider", password=None)
    setup = ClusterRegistration.objects.create(owner=owner, name="Lab cluster draft", host="fixture.invalid")
    foreign = ClusterRegistration.objects.create(owner=outsider, name="Private foreign cluster", host="other.invalid")
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    client.get("/workers/new/")
    return SimpleNamespace(owner=owner, outsider=outsider, setup=setup, foreign=foreign, client=client)


def post(client, payload, *, path="/workers/new/"):
    return client.post(path, payload, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)


def issued(response):
    match = re.search(rb'<pre id="worker-secret">([^<]+)</pre>', response.content)
    assert match is not None
    return match.group(1).decode()


def test_get_forms_and_setup_links_never_create_workers_or_metadata(enrollment):
    before = {model: model.objects.count() for model in (WorkerIdentity, AuditEvent, IdempotencyRecord, Session, Connection)}
    first = enrollment.client.get(f"/workers/new/?setup={enrollment.setup.id}")
    second = enrollment.client.get("/workers/new/")
    assert first.status_code == second.status_code == 200
    assert str(enrollment.setup.id).encode() in first.content and b'name="submission_id"' in first.content
    assert b'name="setup"' in first.content
    assert first.context["form"].initial["submission_id"] != second.context["form"].initial["submission_id"]
    assert "no-store" in first["Cache-Control"] and "private" in first["Cache-Control"]
    assert before == {model: model.objects.count() for model in before}


def test_creation_shows_one_token_only_and_stores_hash_without_plaintext(enrollment, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("registration executed a command"))
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("registration started a process"))
    nonce = uuid4()
    response = post(enrollment.client, {"name": "Protected worker", "submission_id": str(nonce), "setup": str(enrollment.setup.id)})
    assert response.status_code == 201
    secret = issued(response)
    assert secret.startswith("cbw_") and len(secret) > 40
    worker = WorkerIdentity.objects.get()
    assert worker.owner_id == enrollment.owner.id and worker.enrollment_nonce == nonce
    assert worker.token_hash == token_hash(secret) and worker.token_hash != secret
    assert worker.profiles == [] and worker.supported_extensions == [] and worker.last_seen_at is None
    assert not Connection.objects.exists()
    assert "no-store" in response["Cache-Control"] and response["Pragma"] == "no-cache"
    assert response["Referrer-Policy"] == "same-origin"
    for label in ("BROKER_WORKER_CONFIG", "BROKER_WORKER_TOKEN_FILE", "BROKER_WORKER_JOURNAL", str(worker.id)):
        assert label.encode() in response.content
    assert f"/clusters/setup/{enrollment.setup.id}/".encode() in response.content
    assert b"no automatic expiry" in response.content
    detail = enrollment.client.get(f"/workers/{worker.id}/?setup={enrollment.setup.id}")
    assert detail.status_code == 200 and secret.encode() not in detail.content and worker.token_hash.encode() not in detail.content
    assert b"worker-secret" not in detail.content
    repeated_form = enrollment.client.get("/workers/new/")
    assert secret.encode() not in repeated_form.content
    assert not IdempotencyRecord.objects.exists()
    audit = AuditEvent.objects.get()
    assert audit.action == "worker.registered" and audit.object_id == str(worker.id)
    persisted = {"workers": list(WorkerIdentity.objects.values()), "audit": list(AuditEvent.objects.values()),
        "sessions": [item.get_decoded() for item in Session.objects.all()]}
    assert secret not in json.dumps(persisted, default=str)


def test_replaying_nonce_never_reveals_or_creates_another_token(enrollment):
    nonce = str(uuid4())
    response = post(enrollment.client, {"name": "Initial worker", "submission_id": nonce})
    secret = issued(response)
    replay = post(enrollment.client, {"name": "Changed replay name", "submission_id": nonce})
    assert replay.status_code == 409 and secret.encode() not in replay.content
    assert b"SECRET_ALREADY_SHOWN" in replay.content
    assert WorkerIdentity.objects.count() == AuditEvent.objects.count() == 1
    assert WorkerIdentity.objects.get().name == "Initial worker"
    enrollment.client.force_login(enrollment.outsider)
    cross_owner = post(enrollment.client, {"name": "Other owner replay", "submission_id": nonce})
    assert cross_owner.status_code == 409 and secret.encode() not in cross_owner.content
    assert WorkerIdentity.objects.count() == 1


@pytest.mark.parametrize("payload", [{"name": "No nonce"}, {"name": "Bad nonce", "submission_id": "invalid"},
    {"name": "", "submission_id": str(uuid4())}, {"name": "x" * 81, "submission_id": str(uuid4())}])
def test_invalid_forms_create_no_identity_or_secret(enrollment, payload):
    response = post(enrollment.client, payload)
    assert response.status_code == 200 and b"worker-secret" not in response.content
    assert not WorkerIdentity.objects.exists() and not AuditEvent.objects.exists()


def test_csrf_and_agent_credentials_cannot_register_workers(enrollment):
    payload = {"name": "Blocked worker", "submission_id": str(uuid4())}
    missing_csrf = enrollment.client.post("/workers/new/", payload)
    assert missing_csrf.status_code == 403
    project = Project.objects.create(owner=enrollment.owner, name="agent project")
    ProjectCredential.objects.create(project=project, issued_by=project.owner, name="agent", prefix="public-id", token_hash=token_hash("synthetic-agent"),
        scopes=["jobs:write", "jobs:execute"], expires_at=timezone.now() + timedelta(days=1))
    agent = Client()
    denied = agent.post("/workers/new/", payload, HTTP_AUTHORIZATION="Bearer synthetic-agent")
    assert denied.status_code == 403
    assert agent.get("/workers/new/", HTTP_AUTHORIZATION="Bearer synthetic-agent").status_code == 302
    assert not WorkerIdentity.objects.exists()


@pytest.mark.parametrize("method", ["get", "post"])
def test_setup_reference_must_be_owned_for_get_and_post(enrollment, method):
    if method == "get":
        response = enrollment.client.get(f"/workers/new/?setup={enrollment.foreign.id}")
    else:
        response = post(enrollment.client, {"name": "Blocked backlink", "submission_id": str(uuid4()), "setup": str(enrollment.foreign.id)})
    assert response.status_code == 404 and b"Private foreign cluster" not in response.content
    assert not WorkerIdentity.objects.exists()


@pytest.mark.parametrize("hidden", ["", "other"])
def test_setup_query_and_hidden_identity_cannot_disagree(enrollment, hidden):
    other = ClusterRegistration.objects.create(owner=enrollment.owner, name="Other own setup", host="other.fixture.invalid")
    response = post(enrollment.client, {"name": "Mismatched setup", "submission_id": str(uuid4()),
        "setup": str(other.id) if hidden else ""}, path=f"/workers/new/?setup={enrollment.setup.id}")
    assert response.status_code == 400 and not WorkerIdentity.objects.exists()


def test_worker_details_are_owned_read_only_and_escaped(enrollment):
    response = post(enrollment.client, {"name": "<script>alert('worker')</script>", "submission_id": str(uuid4())})
    secret = issued(response)
    worker = WorkerIdentity.objects.get()
    assert b"&lt;script&gt;" in response.content and b"<script>" not in response.content
    detail = enrollment.client.get(f"/workers/{worker.id}/")
    assert detail.status_code == 200 and b"&lt;script&gt;" in detail.content
    assert post(enrollment.client, {}, path=f"/workers/{worker.id}/").status_code == 405
    assert enrollment.client.get(f"/workers/{worker.id}/?setup={enrollment.foreign.id}").status_code == 404
    enrollment.client.force_login(enrollment.outsider)
    assert enrollment.client.get(f"/workers/{worker.id}/").status_code == 404
    assert secret.encode() not in enrollment.client.get("/workers/new/").content
    assert WorkerIdentity.objects.count() == AuditEvent.objects.count() == 1


@pytest.mark.parametrize("different_owners", [False, True])
def test_file_backed_nonce_race_issues_only_one_identity(tmp_path, different_owners):
    script = r'''
import json, os, django
django.setup()
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
from django.core.management import call_command
from django.db import close_old_connections
from compute_broker.control.models import User, WorkerIdentity, AuditEvent
from compute_broker.control.worker_management import issue_worker
from compute_broker.control.errors import BrokerError
call_command('migrate',verbosity=0)
owners=[User.objects.create_user(username='worker-race-'+str(index),password=None) for index in range(4)]
nonce=uuid4();barrier=Barrier(4)
def register(index):
    close_old_connections();barrier.wait(timeout=15)
    try:
        identity=owners[index].id if os.environ['WORKER_TEST_DIFFERENT_OWNERS']=='1' else owners[0].id
        issue_worker(User.objects.get(id=identity),'Concurrent worker',nonce)
        return 'created'
    except BrokerError as error:
        if error.code!='SECRET_ALREADY_SHOWN': raise
        return error.code
    finally: close_old_connections()
with ThreadPoolExecutor(max_workers=4) as executor: results=list(executor.map(register,range(4)))
assert results.count('created')==1 and results.count('SECRET_ALREADY_SHOWN')==3,results
assert WorkerIdentity.objects.count()==AuditEvent.objects.count()==1
print(json.dumps({'created':1,'blocked_replays':3}))
'''
    environment = dict(os.environ, DJANGO_SETTINGS_MODULE="compute_broker.control.settings", BROKER_EXTENSIONS="",
        PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"), WORKER_TEST_DIFFERENT_OWNERS="1" if different_owners else "0",
        BROKER_DB_ENGINE="sqlite", BROKER_DATA_DIR=str(tmp_path / "service"), BROKER_DB_PATH=str(tmp_path / "race.sqlite3"),
        BROKER_ARTIFACT_ROOT=str(tmp_path / "objects"), BROKER_PROFILE="development")
    environment.pop("BROKER_SECRET_KEY_FILE", None)
    result = subprocess.run([sys.executable, "-c", script], env=environment, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {"created": 1, "blocked_replays": 3}
