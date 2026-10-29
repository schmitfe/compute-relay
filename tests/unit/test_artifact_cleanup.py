"""Manual reclamation never erases execution identity or protected bytes."""

from contextlib import contextmanager
from datetime import timedelta
import os
from unittest.mock import patch

from django.core import signing
from django.test import Client
from django.utils import timezone
import pytest

from compute_relay.control import artifact_cleanup as cleanup
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import (Artifact, Attempt, Blob, InputSource, JobFile, LedgerEntry,
    StoragePool, UploadGrant)
from compute_relay.control.storage import begin_transfer, blob_path, store_blob
from test_support.unit_test_management import management, human, form_post
from test_portal_operations import admitted


def output(context, content=b"completed scientific result"):
    job, attempt = admitted(context, outputs=["result.txt"])
    Attempt.objects.filter(id=attempt.id).update(state="succeeded", accounting_state="settled",
        observation={"terminal": True, "state": "succeeded"}, native_identity={"job_id": "17"},
        collection_state="complete", lease_expires_at=None)
    attempt.refresh_from_db()
    blob = store_blob(context.project, content)
    artifact = Artifact.objects.create(attempt=attempt, path="result.txt", blob=blob)
    return job, attempt, blob, artifact


def token(context, job=None):
    value = cleanup.preview(context.project, job)
    return signing.dumps({"project": str(context.project.id), "job": str(job.id) if job else None,
        "actor": str(context.owner.id), "digest": cleanup.preview_digest(value)}, salt=cleanup.SALT)


def test_preview_is_read_only_and_confirm_reclaims_bytes_once(management):
    context = management
    job, attempt, blob, artifact = output(context)
    path = blob_path(blob)
    context.project.refresh_from_db()
    before = context.project.storage_used, context.project.object_count, StoragePool.objects.get(id=1).used
    ledger = list(LedgerEntry.objects.values())
    browser = human(context.owner)
    url = f"/review/{job.id}/cleanup/"
    preview = browser.get(url)
    assert preview.status_code == 200 and preview.context["cleanup"]["bytes"] == len(b"completed scientific result")
    assert preview.content.count(b"<h2>Remote attempt directories</h2>") == 1
    assert b"<" not in preview.content.split(b"<title>", 1)[1].split(b"</title>", 1)[0]
    assert path.exists() and Artifact.objects.filter(id=artifact.id).exists()
    assert b"Local file deletion and remote directory cleanup are separate actions" in preview.content
    assert form_post(browser, url, {"confirm": "delete", "preview_token": preview.context["preview_token"]}).status_code == 302
    assert not path.exists() and not Blob.objects.filter(id=blob.id).exists()
    assert not Artifact.objects.filter(id=artifact.id).exists()
    context.project.refresh_from_db()
    assert (context.project.storage_used, context.project.object_count, StoragePool.objects.get(id=1).used) == (
        before[0] - blob.size_bytes, before[1] - 1, before[2] - blob.size_bytes)
    attempt.refresh_from_db()
    assert attempt.collection_state == "cleaned" and attempt.state == "succeeded" and attempt.native_identity == {"job_id": "17"}
    assert list(LedgerEntry.objects.values()) == ledger
    assert job.files.exists() and job.execution_requests.exists()
    review = browser.get(f"/review/{job.id}/")
    assert b"Manually cleaned up" in review.content
    assert b"Retained output files were manually cleaned up" in review.content
    assert browser.get(url).context["cleanup"]["rows"] == []
    assert form_post(browser, url, {"confirm": "delete", "preview_token": preview.context["preview_token"]}).status_code == 409


@pytest.mark.parametrize("changes", [
    {"state": "running"}, {"state": "submission_uncertain"}, {"accounting_state": "settlement_pending"},
    {"collection_pending": True}, {"collection_state": "failed"}, {"collection_state": "retry_pending"},
    {"lease_expires_at": timezone.now() + timedelta(minutes=1)},
])
def test_unfinished_or_uncertain_work_is_never_a_cleanup_candidate(management, changes):
    job, attempt, blob, artifact = output(management)
    Attempt.objects.filter(id=attempt.id).update(**changes)
    assert cleanup.preview(management.project, job)["rows"] == []
    assert blob_path(blob).exists() and Artifact.objects.filter(id=artifact.id).exists()


@pytest.mark.parametrize("reference", ["upload", "job_input", "definition", "shared_job", "extension"])
def test_uploads_shared_inputs_and_extension_provenance_remain_protected(management, reference):
    job, attempt, blob, artifact = output(management)
    if reference == "upload":
        UploadGrant.objects.create(blob=blob)
    elif reference == "job_input":
        JobFile.objects.create(job=job, path="saved-result.txt", blob=blob, source_artifact=artifact, source_kind="artifact")
    elif reference == "definition":
        InputSource.objects.create(project=management.project, kind="workflow", object_id=job.id,
            path="result.txt", blob=blob, artifact=artifact)
    elif reference == "shared_job":
        other, other_attempt, _, _ = output(management, b"different result")
        Artifact.objects.create(attempt=other_attempt, path="shared.txt", blob=blob)
    else:
        job.extension_name = "environments"
        job.save(update_fields=["extension_name"])
    assert cleanup.preview(management.project, job)["rows"] == []
    assert blob_path(blob).exists() and Artifact.objects.filter(id=artifact.id).exists()


def test_new_reference_invalidates_preview_before_any_deletion(management):
    job, attempt, blob, artifact = output(management)
    approved = token(management, job)
    JobFile.objects.create(job=job, path="now-used.txt", blob=blob, source_artifact=artifact, source_kind="artifact")
    with pytest.raises(BrokerError, match="fresh cleanup preview") as error:
        cleanup.cleanup(management.project, management.owner, approved, job)
    assert error.value.code == "CLEANUP_PREVIEW_CHANGED"
    assert blob_path(blob).exists() and Artifact.objects.filter(id=artifact.id).exists()


@pytest.mark.parametrize("target", ["object", "project", "root"])
def test_cleanup_never_follows_symlinks(management, tmp_path, settings, target):
    job, attempt, blob, artifact = output(management)
    path = blob_path(blob)
    outside = tmp_path / "outside"
    if target == "object":
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    else:
        original = path.parent if target == "project" else settings.ARTIFACT_ROOT
        outside.mkdir(mode=0o700)
        original.rename(outside / "saved")
        original.symlink_to(outside / "saved", target_is_directory=True)
    with pytest.raises(BrokerError):
        cleanup.preview(management.project, job)
    assert Artifact.objects.filter(id=artifact.id).exists()
    if target == "object":
        assert outside.read_bytes() == b"completed scientific result"


def test_changed_file_requires_new_preview_and_hard_links_are_refused(management, tmp_path):
    job, attempt, blob, artifact = output(management)
    approved = token(management, job)
    path = blob_path(blob)
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    with pytest.raises(BrokerError) as error:
        cleanup.cleanup(management.project, management.owner, approved, job)
    assert error.value.code == "CLEANUP_PREVIEW_CHANGED"
    os.link(path, tmp_path / "hard-linked")
    with pytest.raises(BrokerError) as error:
        cleanup.preview(management.project, job)
    assert error.value.code == "CLEANUP_OBJECT_UNSAFE"
    assert path.exists() and Artifact.objects.filter(id=artifact.id).exists()


@pytest.mark.parametrize("crash_after_unlink", [False, True])
def test_interrupted_cleanup_keeps_charges_and_resumes_without_restoring_authority(management, crash_after_unlink):
    job, attempt, blob, artifact = output(management)
    approved = token(management, job)
    management.project.refresh_from_db()
    before = management.project.storage_used
    original = cleanup.os.unlink
    def interrupted(*args, **kwargs):
        if crash_after_unlink:
            original(*args, **kwargs)
        raise OSError("simulated interrupted cleanup")
    with patch.object(cleanup.os, "unlink", side_effect=interrupted):
        with pytest.raises(OSError):
            cleanup.cleanup(management.project, management.owner, approved, job)
    blob.refresh_from_db()
    management.project.refresh_from_db()
    assert blob.cleanup_pending and management.project.storage_used == before
    assert not Artifact.objects.filter(id=artifact.id).exists()
    with pytest.raises(BrokerError) as error:
        begin_transfer(management.project, blob.sha256, blob.size_bytes, "upload:test", uploaded=True)
    assert error.value.code == "CLEANUP_PENDING"
    resumed = cleanup.preview(management.project, job)
    assert len(resumed["rows"]) == 1 and resumed["rows"][0]["pending"]
    assert cleanup.cleanup(management.project, management.owner, token(management, job), job)["objects"] == 1
    assert not Blob.objects.filter(id=blob.id).exists()
    management.project.refresh_from_db()
    assert management.project.storage_used == before - blob.size_bytes


def test_preview_confirmation_requires_human_owner_csrf_and_fresh_bound_token(management):
    job, attempt, blob, artifact = output(management)
    url = f"/projects/{management.project.id}/cleanup/"
    browser = human(management.owner)
    preview = browser.get(url)
    approved = preview.context["preview_token"]
    assert browser.post(url, {"confirm": "delete", "preview_token": approved}).status_code == 403
    assert form_post(browser, url, {"preview_token": approved}).status_code == 400
    assert form_post(browser, url, {"confirm": "delete", "preview_token": "tampered"}).status_code == 409
    assert form_post(human(management.outsider), url, {"confirm": "delete", "preview_token": approved}).status_code == 403
    assert Client().get(url, HTTP_AUTHORIZATION="Bearer " + management.token).status_code == 302
    assert form_post(browser, f"/review/{job.id}/cleanup/", {"confirm": "delete", "preview_token": approved}).status_code == 409
    with patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 601):
        assert form_post(browser, url, {"confirm": "delete", "preview_token": approved}).status_code == 409
    assert blob_path(blob).exists() and Artifact.objects.filter(id=artifact.id).exists()


def test_only_old_unreferenced_internal_objects_are_project_candidates(management):
    blob = store_blob(management.project, b"unused internal package")
    assert cleanup.preview(management.project)["rows"] == []
    Blob.objects.filter(id=blob.id).update(created_at=timezone.now() - timedelta(days=2))
    assert [row["id"] for row in cleanup.preview(management.project)["rows"]] == [str(blob.id)]


def test_optional_module_job_reference_pins_results_even_without_extension_name(management):
    from compute_relay.extensions.environments.models import EnvironmentInspection
    job, attempt, blob, artifact = output(management)
    EnvironmentInspection.objects.create(project=management.project, environment=management.environment,
        credential=management.credential, job=job, attempt=attempt)
    assert job.extension_name == ""
    assert cleanup.preview(management.project, job)["rows"] == []


def test_multiple_paths_to_one_object_keep_cleanup_receipts(management):
    job, attempt, blob, artifact = output(management)
    Artifact.objects.create(attempt=attempt, path="duplicate.txt", blob=blob)
    assert cleanup.cleanup(management.project, management.owner, token(management, job), job)["objects"] == 1
    attempt.refresh_from_db()
    assert {entry["path"] for entry in attempt.log_state["cleanup"]["outputs"]} == {"result.txt", "duplicate.txt"}


def test_project_cleanup_is_bounded_and_can_continue_to_next_batch(management):
    for index in range(cleanup.BATCH_SIZE + 1):
        blob = store_blob(management.project, f"unused-{index}".encode())
        Blob.objects.filter(id=blob.id).update(created_at=timezone.now() - timedelta(days=2))
    first = cleanup.preview(management.project)
    assert len(first["rows"]) == cleanup.BATCH_SIZE and first["more"]
    assert cleanup.cleanup(management.project, management.owner, token(management))["objects"] == cleanup.BATCH_SIZE
    last = cleanup.preview(management.project)
    assert len(last["rows"]) == 1 and not last["more"]
    assert cleanup.cleanup(management.project, management.owner, token(management))["objects"] == 1
    management.project.refresh_from_db()
    assert management.project.storage_used == 0 and management.project.object_count == 0


def test_portal_interruption_explains_how_to_resume(management):
    job, attempt, blob, artifact = output(management)
    browser = human(management.owner)
    url = f"/review/{job.id}/cleanup/"
    preview = browser.get(url)
    with patch.object(cleanup.os, "unlink", side_effect=OSError("disk unavailable")):
        response = form_post(browser, url, {"confirm": "delete", "preview_token": preview.context["preview_token"]})
    assert response.status_code == 503 and b"fresh preview to resume" in response.content
    assert browser.get(url).context["cleanup"]["rows"][0]["pending"]


def test_remote_confirmation_queues_existing_connector_work_without_deleting_local_results(management):
    job, attempt, blob, artifact = output(management)
    connection = management.connections[0]
    connection.backend = "slurm"
    connection.save(update_fields=["backend"])
    browser = human(management.owner)
    url = f"/review/{job.id}/cleanup/"
    preview = browser.get(url)
    assert preview.context["remote_cleanup"]["eligible_count"] == 1
    assert b"Queue previewed remote cleanup" in preview.content
    assert form_post(browser, url, {"cleanup_scope": "remote", "confirm": "delete",
        "preview_token": preview.context["preview_token"]}).status_code == 409
    with patch("compute_relay.transports.openssh.OpenSSHTransport.call_helper", side_effect=AssertionError("Portal must not contact SSH")):
        assert form_post(browser, url, {"cleanup_scope": "remote", "confirm": "delete",
            "preview_token": preview.context["remote_token"]}).status_code == 302
    attempt.refresh_from_db()
    assert attempt.remote_cleanup == {"state": "queued"}
    assert blob_path(blob).exists() and Artifact.objects.filter(id=artifact.id).exists()
    updated = browser.get(url)
    assert not updated.context["remote_cleanup"]["eligible_count"]
    assert b"Cleanup is already queued or running" in updated.content


@pytest.mark.parametrize("state", ["queued", "running", "complete"])
def test_review_does_not_offer_output_recovery_after_remote_cleanup(management, state):
    job, attempt, blob, artifact = output(management)
    cleanup.cleanup(management.project, management.owner, token(management, job), job)
    Attempt.objects.filter(id=attempt.id).update(remote_cleanup={"state": state})
    review = human(management.owner).get(f"/review/{job.id}/")
    assert b"Retry output collection" not in review.content
    assert b"Manually cleaned up" in review.content


def test_project_capacity_explains_burst_refill_and_remaining_retry_time(management):
    management.project.admission_state = {operation: {"tokens": 0, "at": timezone.now().timestamp()}
        for operation in ("create", "execute")}
    management.project.save(update_fields=["admission_state"])
    response = human(management.owner).get(f"/projects/{management.project.id}/")
    assert b"Refills at" in response.content and b"Retry after" in response.content
    assert b"Completed jobs still consume retained job records" not in response.content
    assert b"release their outstanding-work slots" in response.content


def test_cleanup_works_when_storage_and_metadata_capacity_are_exhausted(management, settings):
    job, attempt, blob, artifact = output(management)
    approved = token(management, job)
    settings.PROJECT_STORAGE_LIMIT = settings.GLOBAL_STORAGE_LIMIT = 1
    settings.PROJECT_METADATA_LIMIT = settings.GLOBAL_METADATA_LIMIT = 1
    assert cleanup.cleanup(management.project, management.owner, approved, job)["objects"] == 1
    assert not blob_path(blob).exists()


def test_reference_added_after_intent_keeps_bytes_and_cancels_that_deletion(management):
    job, attempt, blob, artifact = output(management)
    approved = token(management, job)
    original = cleanup.object_directory
    calls = 0
    @contextmanager
    def changed_reference(project):
        nonlocal calls
        calls += 1
        if calls == 2:
            JobFile.objects.create(job=job, path="new-generated-input.txt", blob=blob, source_kind="generated")
        with original(project) as directory:
            yield directory
    with patch.object(cleanup, "object_directory", changed_reference):
        result = cleanup.cleanup(management.project, management.owner, approved, job)
    assert result == {"objects": 0, "bytes": 0, "newly_referenced": 1}
    blob.refresh_from_db()
    assert not blob.cleanup_pending and blob_path(blob).exists()


def test_explicit_upload_cannot_regrant_an_interrupted_cleanup_object(management):
    from compute_relay.control.storage import store_uploaded_blob
    job, attempt, blob, artifact = output(management)
    Blob.objects.filter(id=blob.id).update(cleanup_pending=True)
    # Model publication returning just before cleanup recorded its intent.
    with patch("compute_relay.control.storage.store_blob", return_value=blob):
        with pytest.raises(BrokerError) as error:
            store_uploaded_blob(management.project, b"completed scientific result")
    assert error.value.code == "CLEANUP_PENDING"
    assert not UploadGrant.objects.filter(blob=blob).exists()
