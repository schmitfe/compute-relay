"""Compaction frees draft metadata without erasing execution identities."""

from datetime import timedelta
from copy import deepcopy
import json

from django.test import Client
from django.utils import timezone
import pytest

from compute_relay.control import services
from compute_relay.control.errors import BrokerError
from compute_relay.control.maintenance import compact_draft_jobs
from compute_relay.control.metadata import metadata_usage, retire_metadata
from compute_relay.control.models import Blob, Job, MetadataUsage
from test_support.unit_test_control import broker, budgeted, execution, prepared_job


def old(job):
    Job.objects.filter(id=job.id).update(created_at=timezone.now() - timedelta(days=60))
    return job


def compact(broker, **options):
    return compact_draft_jobs(owner=broker.owner, before=timezone.now() - timedelta(days=30), **options)


def test_dry_run_preserves_draft_and_apply_retains_a_bounded_identity(broker):
    job = old(prepared_job(broker))
    before = metadata_usage(broker.project)
    files, blobs, digest = job.files.count(), Blob.objects.count(), job.manifest_sha256
    cached = services.idempotent(broker.project, "job-fixture", "old-request", {},
        lambda: ({"id": str(job.id)}, 201))
    assert compact(broker) == {"candidates": 1, "archived": 0, "skipped": 0}
    assert job.files.count() == files
    assert compact(broker, apply=True)["archived"] == 1
    job.refresh_from_db()
    assert job.state == "archived" and job.name == "synthetic"
    assert job.manifest is None and job.manifest_sha256 == digest
    assert job.files.count() == 0 and Blob.objects.count() == blobs
    assert not MetadataUsage.objects.filter(project=broker.project, kind="job", object_id=str(job.id)).exists()
    assert MetadataUsage.objects.filter(project=broker.project, kind="retired_job", object_id=str(job.id)).exists()
    assert metadata_usage(broker.project)["logical_bytes"] < before["logical_bytes"]
    # Reusing a request identity returns its existing result, never another job.
    assert services.idempotent(broker.project, "job-fixture", "old-request", {},
        lambda: pytest.fail("An archived request must never replay")) == cached
    for action in (lambda: services.prepare_job(job), lambda: services.request_execution(job, broker.credential)):
        with pytest.raises(BrokerError) as error:
            action()
        assert error.value.code == "JOB_ARCHIVED"


def test_archival_preserves_reviews_attempts_and_live_revision_parents(broker):
    budgeted(broker)
    live = old(prepared_job(broker))
    execution(broker, live)
    broker.grant.policy["mode"] = "approval_required"
    broker.grant.save(update_fields=["policy"])
    waiting = old(prepared_job(broker))
    execution(broker, waiting)
    parent = old(prepared_job(broker))
    created, _ = services.create_job(broker.credential, parent.request, previous=parent)
    child = Job.objects.get(id=created["id"])
    assert compact(broker, apply=True)["archived"] == 0
    for job in (live, waiting, parent):
        job.refresh_from_db()
        assert job.state == "prepared" and job.files.exists()
    child.refresh_from_db()
    assert child.state == "draft"


def test_full_tombstone_cap_rolls_back_all_compaction_changes(broker, settings):
    job = old(prepared_job(broker))
    before = metadata_usage(broker.project)
    files = job.files.count()
    settings.MAX_PROJECT_RETIRED_JOBS = 0
    with pytest.raises(BrokerError) as error:
        compact(broker, apply=True)
    assert error.value.code == "METADATA_QUOTA"
    job.refresh_from_db()
    assert job.state == "prepared" and job.files.count() == files
    assert metadata_usage(broker.project) == before


def test_other_owner_jobs_are_never_selected(broker):
    job = old(prepared_job(broker))
    assert compact_draft_jobs(owner=broker.outsider, before=timezone.now(), apply=True)["archived"] == 0
    job.refresh_from_db()
    assert job.state == "prepared"


def test_archived_identity_remains_readable_but_cannot_be_prepared_or_executed(broker):
    job = old(prepared_job(broker))
    document = deepcopy(job.request)
    assert compact(broker, apply=True)["archived"] == 1
    agent = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    detail = agent.get(f"/api/v1/jobs/{job.id}")
    assert detail.status_code == 200 and detail.json()["state"] == "archived"
    assert detail.json()["manifest"] is None and detail.json()["files"] == []
    assert agent.get(f"/api/v1/jobs/{job.id}/package").status_code == 410
    for endpoint, payload in [("prepare", {}), ("execution-requests", {}), ("revisions", document)]:
        response = agent.post(f"/api/v1/jobs/{job.id}/{endpoint}", data=json.dumps(payload),
            content_type="application/json", HTTP_IDEMPOTENCY_KEY="archive-" + endpoint)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "JOB_ARCHIVED"
    human = Client()
    human.force_login(broker.owner, backend="compute_relay.control.local_auth.LocalOwnerBackend")
    review = human.get(f"/review/{job.id}/")
    assert review.status_code == 200 and b"package metadata was archived" in review.content
    assert human.get(f"/projects/{broker.project.id}/").status_code == 200


def test_archival_shrinks_above_ceiling_upgrade_without_reopening_admissions(broker, settings):
    first = old(prepared_job(broker))
    second = old(prepared_job(broker))
    before = metadata_usage(broker.project)["logical_bytes"]
    settings.PROJECT_METADATA_LIMIT = 1
    settings.GLOBAL_METADATA_LIMIT = 1
    assert compact(broker, apply=True, limit=1)["archived"] == 1
    after = metadata_usage(broker.project)["logical_bytes"]
    assert 1 < after < before
    assert Job.objects.filter(id__in=[first.id, second.id], state="archived").count() == 1
    remaining = Job.objects.filter(id__in=[first.id, second.id], state="prepared").get()
    with pytest.raises(BrokerError) as error:
        services.create_job(broker.credential, remaining.request)
    assert error.value.code == "METADATA_QUOTA"


def test_retirement_cannot_create_or_enlarge_a_charge(broker):
    job = old(prepared_job(broker))
    before = metadata_usage(broker.project)
    with pytest.raises(BrokerError) as error:
        retire_metadata(broker.project, "nonexistent", 100)
    assert error.value.code == "METADATA_NOT_SHRINKING"
    MetadataUsage.objects.filter(project=broker.project, kind="job", object_id=str(job.id)).update(size_bytes=200)
    with pytest.raises(BrokerError) as error:
        retire_metadata(broker.project, job.id, 200)
    assert error.value.code == "METADATA_NOT_SHRINKING"
    assert MetadataUsage.objects.get(project=broker.project, kind="job", object_id=str(job.id)).size_bytes == 200


def test_global_retired_identity_cap_still_blocks_shrinking_over_quota(broker, settings):
    job = old(prepared_job(broker))
    before = metadata_usage(broker.project)
    settings.MAX_INSTALLATION_RETIRED_JOBS = 0
    settings.PROJECT_METADATA_LIMIT = 1
    with pytest.raises(BrokerError) as error:
        compact(broker, apply=True)
    assert error.value.code == "METADATA_QUOTA"
    job.refresh_from_db()
    assert job.state == "prepared" and job.files.exists()
    assert metadata_usage(broker.project) == before


def test_protected_revision_parent_does_not_consume_small_archival_batch(broker):
    parent = old(prepared_job(broker))
    created, _ = services.create_job(broker.credential, parent.request, previous=parent)
    child = Job.objects.get(id=created["id"])
    eligible = old(prepared_job(broker))
    Job.objects.filter(id=parent.id).update(created_at=timezone.now() - timedelta(days=90))
    assert compact(broker, apply=True, limit=1) == {"candidates": 1, "archived": 1, "skipped": 0}
    parent.refresh_from_db()
    child.refresh_from_db()
    eligible.refresh_from_db()
    assert parent.state == "prepared" and parent.files.exists()
    assert child.state == "draft" and eligible.state == "archived"


def test_fresh_locked_child_check_protects_parent_selected_before_child_creation(broker, monkeypatch):
    from django.db.models.query import QuerySet
    parent = old(prepared_job(broker))
    original_get = QuerySet.get
    inserted = []
    def insert_child_before_lock(queryset, *args, **kwargs):
        # Candidate IDs have already been sliced before the project lock query.
        if queryset.model is Project and kwargs.get("owner") == broker.owner and not inserted:
            inserted.append(True)
            services.create_job(broker.credential, parent.request, previous=parent)
        return original_get(queryset, *args, **kwargs)
    from compute_relay.control.models import Project
    monkeypatch.setattr(QuerySet, "get", insert_child_before_lock)
    assert compact(broker, apply=True, limit=1) == {"candidates": 0, "archived": 0, "skipped": 1}
    parent.refresh_from_db()
    assert parent.state == "prepared" and parent.files.exists()


def test_compacted_keys_do_not_exhaust_active_key_quota(broker, settings):
    from compute_relay.control.metadata import compact_idempotency
    settings.MAX_PROJECT_IDEMPOTENCY_KEYS = 1
    action = lambda: ({"ok": True}, 200)
    services.idempotent(broker.project, "test", "first", {}, action)
    compact_idempotency(before=timezone.now() + timedelta(seconds=1))
    assert services.idempotent(broker.project, "test", "second", {}, action)[1] == 200
    assert services.idempotent(broker.project, "test", "first", {}, action)[0]["error"]["code"] == "IDEMPOTENCY_RETIRED"
