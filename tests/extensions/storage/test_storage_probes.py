"""Opt-in leased statvfs maintenance, without real SSH or remote storage."""

import copy
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from compute_relay.control.auth import token_hash
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Job, WorkerIdentity
from compute_relay.extensions.storage import services
from compute_relay.extensions.storage.models import StorageObservationLease
from compute_relay.extensions.storage.protocol import probe_context
from compute_relay.extensions.storage.remote import REMOTE_SOURCE
from compute_relay.extensions.storage.worker import execute
from compute_relay.worker.enrollment import profile_metadata, resolve_connection
from compute_relay.worker.journal import Journal
from compute_relay.worker.runner import Worker


@pytest.fixture
def probes(resources, tmp_path):
    root = tmp_path / "enrolled"
    root.mkdir()
    profile = {"backend": "slurm", "allowed_hosts": ["storage.invalid"], "allowed_users": ["researcher"], "allowed_ports": [22],
               "remote_root_prefixes": [str(root)], "ssh": {"identity_file": "/private/not-readable-by-agent",
                                                            "known_hosts": "/private/pinned-hosts"}}
    public = {"backend": "slurm", "credential_profile": "protected", "ssh": {"host": "storage.invalid", "user": "researcher", "port": 22},
              "remote_root": str(root) + "/jobs"}
    private, public, revision = resolve_connection(public, {"protected": profile})
    resources.worker.profiles = [profile_metadata("protected", profile)]
    resources.worker.supported_extensions = ["storage"]
    resources.worker.save(update_fields=["profiles", "supported_extensions"])
    resources.connection.configuration, resources.connection.revision_sha256 = public, revision
    resources.connection.backend = "slurm"
    resources.connection.save(update_fields=["configuration", "revision_sha256", "backend"])
    target = services.register_target(resources.owner, resources.connection, "staging-root-0", "Test metadata only",
                                     project=resources.project)
    resources.target, resources.profile, resources.private, resources.public, resources.root = target, profile, private, public, root
    return resources


def lease(probes):
    services.set_probe_enabled(probes.owner, probes.target.pool, True)
    document = services.lease_work(probes.worker)
    assert document is not None
    return {**document, "operation": "extension", "extension": "storage"}


def sample():
    return {"schema_version": "1.0", "source": "statvfs", "status": "available", "observed_at": timezone.now().isoformat(),
            "filesystem_id": "1:2", "identity_verified": False, "total_bytes": 10000000000, "available_bytes": 8000000000,
            "total_inodes": 10000, "available_inodes": 8000, "quota": {"status": "unknown"}}


def report(document, observation=None):
    context = document["context"]
    return {"lease_id": document["id"], "generation": document["generation"], "target_id": context["target_id"],
            "profile_revision_sha256": context["profile_revision_sha256"],
            "connection_revision_sha256": context["connection_revision_sha256"], "observation": observation or sample()}


class CaptureClient:
    def __init__(self):
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"accepted": True}


def test_probes_are_off_until_owner_opt_in_and_worker_advertises_support(probes):
    assert services.lease_work(probes.worker) is None
    assert not StorageObservationLease.objects.exists()
    services.set_probe_enabled(probes.owner, probes.target.pool, True)
    probes.worker.supported_extensions = []
    probes.worker.save(update_fields=["supported_extensions"])
    assert services.lease_work(probes.worker) is None
    assert not Job.objects.exists()


def test_pool_probes_coalesce_expire_and_fence_old_generations(probes):
    first = lease(probes)
    assert services.lease_work(probes.worker) is None
    StorageObservationLease.objects.filter(id=first["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    probes.target.pool.next_probe_at = timezone.now() - timedelta(seconds=1)
    probes.target.pool.save(update_fields=["next_probe_at"])
    second = services.lease_work(probes.worker)
    assert second["generation"] == first["generation"] + 1
    assert StorageObservationLease.objects.filter(state="leased").count() == 1
    with pytest.raises(BrokerError) as error:
        services.report_probe(probes.worker, probes.target.pool_id, report(first))
    assert error.value.code == "STALE_LEASE"


def test_report_is_idempotent_conflicts_are_rejected_and_worker_lane_yields(probes):
    document = lease(probes)
    payload = report(document)
    response = services.report_probe(probes.worker, probes.target.pool_id, payload)
    assert services.report_probe(probes.worker, probes.target.pool_id, payload) == response
    assert services.lease_work(probes.worker) is None
    changed = copy.deepcopy(payload)
    changed["observation"]["available_bytes"] -= 1
    with pytest.raises(BrokerError) as error:
        services.report_probe(probes.worker, probes.target.pool_id, changed)
    assert error.value.code == "STORAGE_REPORT_CONFLICT"
    assert StorageObservationLease.objects.get().reported_at is not None
    probes.target.pool.refresh_from_db()
    assert probes.target.pool.observation["available_bytes"] == payload["observation"]["available_bytes"]
    # Even a due pool yields after maintenance, so repeated capacity checks
    # cannot monopolize the worker before job observations and accounting.
    probes.target.pool.next_probe_at = timezone.now() - timedelta(seconds=1)
    probes.target.pool.save(update_fields=["next_probe_at"])
    assert services.lease_work(probes.worker) is None
    StorageObservationLease.objects.update(reported_at=timezone.now() - timedelta(seconds=16))
    assert services.lease_work(probes.worker) is not None


def test_pausing_and_profile_changes_fence_inflight_reports(probes):
    document = lease(probes)
    services.set_probe_enabled(probes.owner, probes.target.pool, False)
    with pytest.raises(BrokerError) as error:
        services.report_probe(probes.worker, probes.target.pool_id, report(document))
    assert error.value.code == "STALE_LEASE"
    document = lease(probes)
    probes.connection.revision_sha256 = "e" * 64
    probes.connection.save(update_fields=["revision_sha256"])
    with pytest.raises(BrokerError) as error:
        services.report_probe(probes.worker, probes.target.pool_id, report(document))
    assert error.value.code == "REVISION_CHANGED"


def test_probe_report_requires_owning_worker_and_new_sample_provenance(probes):
    document = lease(probes)
    other = WorkerIdentity.objects.create(owner=probes.outsider, name="other", token_hash=token_hash("other-storage-worker"), supported_extensions=["storage"])
    with pytest.raises(BrokerError) as error:
        services.report_probe(other, probes.target.pool_id, report(document))
    assert error.value.status == 404
    old = sample()
    old["observed_at"] = (timezone.now() - timedelta(minutes=1)).isoformat()
    with pytest.raises(BrokerError) as error:
        services.report_probe(probes.worker, probes.target.pool_id, report(document, old))
    assert error.value.code == "STALE_OBSERVATION"


def test_http_probe_controls_require_owner_session_csrf_and_worker_auth(probes):
    project_path = f"/projects/{probes.project.id}/storage/"
    url = project_path + f"pools/{probes.target.pool_id}/probes/"
    assert Client().post(url, {"enabled": "true"}, HTTP_AUTHORIZATION="Bearer " + probes.token).status_code == 403
    foreign = Client()
    foreign.force_login(probes.outsider)
    assert foreign.post(url, {"enabled": "true"}).status_code == 403
    human = Client(enforce_csrf_checks=True)
    human.force_login(probes.owner)
    assert human.post(url, {"enabled": "true"}).status_code == 403
    human.get(project_path)
    assert human.post(url, {"enabled": "true"}, HTTP_X_CSRFTOKEN=human.cookies["csrftoken"].value).status_code == 302
    response = Client().post("/internal/v1/lease", "{}", content_type="application/json", HTTP_AUTHORIZATION="Bearer storage-worker")
    assert response.status_code == 200 and response.json()["lease"]["extension"] == "storage"
    document = response.json()["lease"]
    report_url = f"/internal/v1/extensions/storage/pools/{probes.target.pool_id}/report"
    assert Client().post(report_url, json.dumps(report(document)), content_type="application/json",
                         HTTP_AUTHORIZATION="Bearer " + probes.token).status_code == 401
    accepted = Client().post(report_url, json.dumps(report(document)), content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer storage-worker")
    assert accepted.status_code == 200, accepted.content


def test_worker_resolves_root_privately_and_fixed_helper_reports_real_tmp_metadata(probes, tmp_path):
    document = lease(probes)
    requests = []

    class Transport:
        def call_helper(self, source, request):
            requests.append(request)
            assert source == REMOTE_SOURCE
            assert "identity_file" not in json.dumps(request)
            result = subprocess.run([sys.executable, "-I", "-c", source], input=json.dumps(request).encode(), capture_output=True, timeout=5)
            assert result.returncode == 0
            return json.loads(result.stdout)

    class HttpClient:
        def request(self, method, path, body):
            response = Client().post(path, json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION="Bearer storage-worker")
            assert response.status_code == 200, response.content
            return response.json()

    worker = Worker(HttpClient(), {}, Journal(str(tmp_path / "journal")), credential_profiles={"protected": probes.profile},
                    extensions=["storage"], backend_factory=lambda configuration: SimpleNamespace(transport=Transport()))
    worker.execute(document)
    assert requests[0]["root"] == str(probes.root)
    probes.target.pool.refresh_from_db()
    data = probes.target.pool.observation
    assert data["source"] == "statvfs" and data["status"] == "available"
    assert data["total_bytes"] > 0 and data["available_bytes"] >= 0
    assert data["identity_verified"] is False and data["quota"]["status"] == "unknown"
    assert not Job.objects.exists()


def test_fake_backend_never_fabricates_capacity_or_calls_any_transport(probes, tmp_path):
    profile = {**probes.profile, "backend": "fake"}
    public = {**probes.public, "backend": "fake"}
    public.pop("credential_profile_revision_sha256")
    _private, public, revision = resolve_connection(public, {"protected": profile}, refresh=True)
    document = lease(probes)
    document["configuration"] = public
    document["context"]["connection_revision_sha256"] = revision
    document["context"]["profile_revision_sha256"] = profile_metadata("protected", profile)["revision_sha256"]
    client = CaptureClient()
    worker = Worker(client, {}, Journal(str(tmp_path / "fake-journal")), credential_profiles={"protected": profile}, extensions=["storage"],
                    backend_factory=lambda *_args: pytest.fail("fake storage performed an operation"))
    execute(worker, document)
    observation = client.calls[0][0][2]["observation"]
    assert observation["error_code"] == "SIMULATED_STORAGE_NOT_PROBED"
    assert observation["available_bytes"] is None and observation["available_inodes"] is None


def test_unknown_root_or_changed_fingerprint_never_reaches_ssh_or_leaks_diagnostics(probes, tmp_path):
    document = lease(probes)
    document["context"]["root_alias"] = "not-advertised"
    client = CaptureClient()
    worker = Worker(client, {}, Journal(str(tmp_path / "invalid-journal")), credential_profiles={"protected": probes.profile}, extensions=["storage"],
                    backend_factory=lambda *_args: pytest.fail("unapproved storage root reached transport"))
    execute(worker, document)
    assert client.calls[0][0][2]["observation"]["error_code"] == "STORAGE_PROBE_UNAVAILABLE"
    assert "not-readable-by-agent" not in json.dumps(client.calls)
    document["context"]["root"] = "/agent-selected/path"
    with pytest.raises(ValueError):
        execute(worker, document)
    assert len(client.calls) == 1


def test_remote_helper_never_scans_reads_files_or_follows_symlink_roots(probes):
    document = lease(probes)
    request = {"operation": "probe_storage", "context": document["context"], "root": str(probes.root), "expected_filesystem_id": None}
    guard = """import datetime,json,os,re,sys,time,uuid,builtins
def forbidden(*args,**kwargs): raise AssertionError('content or scan attempted')
real_open=os.open
def directories_only(path,flags,*args,**kwargs):
    if not flags & os.O_DIRECTORY or flags & (os.O_CREAT|os.O_WRONLY|os.O_RDWR): raise AssertionError('non-directory open')
    return real_open(path,flags,*args,**kwargs)
os.open=directories_only
os.scandir=os.listdir=builtins.open=forbidden
"""
    result = subprocess.run([sys.executable, "-I", "-c", guard + REMOTE_SOURCE], input=json.dumps(request).encode(), capture_output=True, timeout=5)
    assert json.loads(result.stdout)["observation"]["status"] == "available"
    alias = probes.root.parent / "linked-root"
    alias.symlink_to(probes.root, target_is_directory=True)
    request["root"] = str(alias)
    result = subprocess.run([sys.executable, "-I", "-c", REMOTE_SOURCE], input=json.dumps(request).encode(), capture_output=True, timeout=5)
    assert json.loads(result.stdout)["observation"]["status"] == "unavailable"
