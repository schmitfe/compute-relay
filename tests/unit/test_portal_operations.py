"""Human operations reuse existing authority and state transitions; no cluster I/O."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.models import Artifact, Attempt, ExecutionRequest, IdempotencyRecord, Job, LedgerEntry
from compute_relay.control.metadata import metadata_usage
from compute_relay.control.storage import blob_path, store_blob
from test_support.unit_test_management import management, human, form_post, prepared


def admitted(context, *, outputs=None):
    job = prepared(context, outputs=outputs)
    execution, _ = services.request_execution(job, context.credential)
    response, _ = services.decide(job, context.owner, {
        'execution_request_id': execution['id'], 'manifest_sha256': job.manifest_sha256, 'approved': True})
    return job, Attempt.objects.get(id=response['attempt_id'])


def action_url(job, attempt, action):
    return f'/review/{job.id}/attempts/{attempt.id}/{action}/'


def fill_metadata_capacity(context, settings):
    services.idempotent(context.project, 'existing-action', 'existing-receipt', {}, lambda: ({}, 200))
    before = metadata_usage(context.project)
    settings.PROJECT_METADATA_LIMIT = before['logical_bytes']
    settings.GLOBAL_METADATA_LIMIT = before['logical_bytes']
    settings.MAX_PROJECT_IDEMPOTENCY_KEYS = settings.MAX_INSTALLATION_IDEMPOTENCY_KEYS = 1
    return before


def test_job_navigation_finds_old_unresolved_work_and_keeps_live_view(management):
    context = management
    job, attempt = admitted(context)
    Job.objects.filter(id=job.id).update(created_at=timezone.now() - timedelta(days=1))
    Job.objects.bulk_create([Job(project=context.project, credential=context.credential, request={'name': f'new draft {i}'}) for i in range(101)])
    browser = human(context.owner)
    pages = [('/', '/ui/live/dashboard/'),
             (f'/projects/{context.project.id}/', f'/ui/live/project/{context.project.id}/')]
    for page_url, live_url in pages:
        first = browser.get(page_url)
        assert len(first.context['jobs']) == 50
        assert first.context['job_page'].paginator.count == 102
        page = browser.get(page_url, {'page': 3})
        assert job.id in {item.id for item in page.context['jobs']}
        assert b'page=3' in page.content
        filtered = browser.get(page_url, {'job_state': 'unresolved'})
        assert {item.id for item in filtered.context['jobs']} == {job.id}
        polling = browser.get(live_url, {'job_state': 'unresolved'}).json()['fragments']['live-job-list']
        assert str(job.id) in polling and 'new draft' not in polling
        later = browser.get(live_url, {'page': 2}).json()['fragments']['live-job-list']
        assert 'Page 2 of 3' in later
        assert f'href="{page_url}?job_state=all&amp;page=3#jobs"' in later
        assert '/ui/live/' not in later


@pytest.mark.parametrize('state,accounting,collection,pending', [
    ('succeeded', 'settlement_pending', 'complete', False),
    ('succeeded', 'settled', 'failed', False),
    ('succeeded', 'settled', 'pending', True),
    ('authorized', 'reserved', 'pending', False),
])
def test_unresolved_filter_keeps_accounting_collection_and_uncertainty(management, state, accounting, collection, pending):
    job, attempt = admitted(management)
    Attempt.objects.filter(id=attempt.id).update(state=state, accounting_state=accounting,
        collection_state=collection, collection_pending=pending)
    response = human(management.owner).get('/', {'job_state': 'unresolved'})
    assert {item.id for item in response.context['jobs']} == {job.id}
    assert str(job.id) not in human(management.outsider).get('/', {'job_state': 'unresolved'}).content.decode()


def test_review_filter_and_invalid_navigation_are_safe(management):
    job = prepared(management)
    services.request_execution(job, management.credential)
    browser = human(management.owner)
    response = browser.get('/', {'job_state': 'awaiting_review'})
    assert {item.id for item in response.context['jobs']} == {job.id}
    invalid = browser.get('/', {'job_state': '<script>', 'page': 'bad'})
    assert invalid.status_code == 200 and invalid.context['job_state'] == 'all'
    assert invalid.context['job_page'].number == 1


def test_stale_review_preserves_escaped_comment_only_for_matching_request(management):
    job = prepared(management)
    execution, _ = services.request_execution(job, management.credential)
    other, _ = services.request_execution(job, management.credential)
    comment = '</textarea><script>alert("review")</script>'
    response = form_post(human(management.owner), f'/review/{job.id}/', {
        'decision': 'reject', 'execution_request_id': execution['id'], 'manifest_sha256': '0' * 64,
        'idempotency_key': str(uuid4()), 'comment': comment})
    assert response.status_code == 200
    assert b'The reviewed package no longer matches.' in response.content
    assert b'&lt;/textarea&gt;&lt;script&gt;' in response.content
    assert comment.encode() not in response.content
    assert response.content.count(b'&lt;/textarea&gt;&lt;script&gt;') == 1
    assert ExecutionRequest.objects.filter(job=job, state='awaiting_approval').count() == 2
    assert not Attempt.objects.filter(job=job).exists()


def test_comment_survives_a_decision_race_without_reopening_request(management):
    job = prepared(management)
    execution, _ = services.request_execution(job, management.credential)
    services.decide(job, management.owner, {'execution_request_id': execution['id'],
        'manifest_sha256': job.manifest_sha256, 'approved': False})
    comment = 'Keep this instruction after the other decision.'
    response = form_post(human(management.owner), f'/review/{job.id}/', {
        'decision': 'reject', 'execution_request_id': execution['id'], 'manifest_sha256': job.manifest_sha256,
        'idempotency_key': str(uuid4()), 'comment': comment})
    assert b'<textarea readonly>' + comment.encode() in response.content
    assert b'Approve one execution' not in response.content
    assert ExecutionRequest.objects.get(id=execution['id']).state == 'rejected'


@pytest.mark.parametrize('action', ['cancel', 'collect'])
def test_human_operations_require_owner_csrf_and_post(management, action):
    job, attempt = admitted(management)
    url = action_url(job, attempt, action)
    owner = human(management.owner)
    assert owner.get(url).status_code == 405
    assert owner.post(url, {'idempotency_key': str(uuid4())}).status_code == 403
    assert form_post(human(management.outsider), url, {'idempotency_key': str(uuid4())}).status_code == 403
    anonymous = Client(enforce_csrf_checks=True)
    anonymous.get('/login/')
    assert anonymous.post(url, {'idempotency_key': str(uuid4())},
        HTTP_X_CSRFTOKEN=anonymous.cookies['csrftoken'].value,
        HTTP_AUTHORIZATION='Bearer ' + management.token).status_code == 403
    attempt.refresh_from_db()
    assert attempt.state == 'authorized' and not attempt.cancel_requested and not attempt.collection_pending


def test_wrong_attempt_id_cannot_target_another_job(management):
    job, attempt = admitted(management)
    other = prepared(management)
    browser = human(management.owner)
    assert form_post(browser, action_url(other, attempt, 'cancel'), {'idempotency_key': str(uuid4())}).status_code == 404
    attempt.refresh_from_db()
    assert attempt.state == 'authorized'


def test_uncertain_cancellation_preserves_hold_and_replays_without_duplicate_work(management):
    job, attempt = admitted(management)
    attempt.dispatch_started = True
    attempt.save(update_fields=['dispatch_started'])
    management.grant.refresh_from_db()
    balance = (management.grant.reserved_cpu, management.grant.spent_cpu)
    ledgers = LedgerEntry.objects.count()
    browser = human(management.owner)
    nonce = {'idempotency_key': str(uuid4())}
    for _ in range(2):
        assert form_post(browser, action_url(job, attempt, 'cancel'), nonce).status_code == 302
    attempt.refresh_from_db()
    management.grant.refresh_from_db()
    assert attempt.cancel_requested and attempt.native_identity is None and attempt.state == 'authorized'
    assert (management.grant.reserved_cpu, management.grant.spent_cpu) == balance
    assert LedgerEntry.objects.count() == ledgers
    assert Attempt.objects.filter(job=job).count() == 1
    assert not IdempotencyRecord.objects.filter(operation__startswith='human:').exists()


def test_cancellation_works_with_full_metadata_and_receipt_quotas(management, settings):
    job, attempt = admitted(management)
    Attempt.objects.filter(id=attempt.id).update(dispatch_started=True)
    before = fill_metadata_capacity(management, settings)
    management.grant.refresh_from_db()
    balance = (management.grant.reserved_cpu, management.grant.spent_cpu)
    ledger_count = LedgerEntry.objects.count()
    browser = human(management.owner)
    for _ in range(2):
        assert form_post(browser, action_url(job, attempt, 'cancel')).status_code == 302
    attempt.refresh_from_db()
    management.grant.refresh_from_db()
    assert attempt.cancel_requested and attempt.native_identity is None and attempt.state == 'authorized'
    assert (management.grant.reserved_cpu, management.grant.spent_cpu) == balance
    assert LedgerEntry.objects.count() == ledger_count
    assert Attempt.objects.filter(job=job).count() == 1
    assert IdempotencyRecord.objects.count() == 1
    assert metadata_usage(management.project) == before


def test_collection_recovery_at_full_metadata_uses_same_attempt_and_never_admits_compute(management, settings):
    job, attempt = admitted(management, outputs=['result.txt'])
    Attempt.objects.filter(id=attempt.id).update(state='succeeded', dispatch_started=True,
        native_identity={'job_id': '123'}, observation={'terminal': True, 'state': 'succeeded'}, collection_state='failed')
    management.grant.refresh_from_db()
    balance = (management.grant.reserved_cpu, management.grant.spent_cpu)
    count = LedgerEntry.objects.count()
    before = fill_metadata_capacity(management, settings)
    browser = human(management.owner)
    assert b'Retry output collection' in browser.get(f'/review/{job.id}/').content
    with patch.object(services, 'authorize', side_effect=AssertionError('Recovery must not admit compute')):
        for _ in range(2):
            assert form_post(browser, action_url(job, attempt, 'collect')).status_code == 302
    attempt.refresh_from_db()
    management.grant.refresh_from_db()
    assert attempt.collection_pending and attempt.collection_state == 'pending'
    assert Attempt.objects.filter(job=job).count() == 1
    assert LedgerEntry.objects.count() == count
    assert (management.grant.reserved_cpu, management.grant.spent_cpu) == balance
    assert metadata_usage(management.project) == before
    assert IdempotencyRecord.objects.count() == 1
    assert b'Retry output collection' not in browser.get(f'/review/{job.id}/').content


def test_api_attempt_cancellation_at_full_metadata_needs_no_new_receipt(management, settings):
    job, attempt = admitted(management)
    Attempt.objects.filter(id=attempt.id).update(dispatch_started=True)
    before = fill_metadata_capacity(management, settings)
    management.grant.refresh_from_db()
    balance = (management.grant.reserved_cpu, management.grant.spent_cpu)
    ledger_count = LedgerEntry.objects.count()
    agent = Client(HTTP_AUTHORIZATION='Bearer ' + management.token)
    endpoint = f'/api/v1/attempts/{attempt.id}/cancel'
    assert agent.post(endpoint, '{"unexpected":true}', content_type='application/json').status_code == 400
    for _ in range(2):
        response = agent.post(endpoint, '{}', content_type='application/json')
        assert response.status_code == 202 and response.json()['id'] == str(attempt.id)
    attempt.refresh_from_db()
    management.grant.refresh_from_db()
    assert attempt.cancel_requested and attempt.state == 'authorized' and attempt.native_identity is None
    assert (management.grant.reserved_cpu, management.grant.spent_cpu) == balance
    assert LedgerEntry.objects.count() == ledger_count
    assert Attempt.objects.filter(job=job).count() == 1
    assert metadata_usage(management.project) == before
    assert IdempotencyRecord.objects.count() == 1


@pytest.mark.parametrize('human_cancel', [False, True])
def test_pending_request_cancellation_at_full_metadata_releases_hold_once(management, settings, human_cancel):
    job = prepared(management)
    execution, _ = services.request_execution(job, management.credential)
    before = fill_metadata_capacity(management, settings)
    ledger_count = LedgerEntry.objects.count()
    if human_cancel:
        browser = human(management.owner)
    else:
        agent = Client(HTTP_AUTHORIZATION='Bearer ' + management.token)
    for _ in range(2):
        if human_cancel:
            response = form_post(browser, f'/review/{job.id}/', {
                'decision': 'cancel', 'execution_request_id': execution['id'], 'manifest_sha256': job.manifest_sha256})
            assert response.status_code == 302
        else:
            response = agent.post(f'/api/v1/jobs/{job.id}/execution-requests/{execution["id"]}/cancel',
                '{}', content_type='application/json')
            assert response.status_code == 202 and response.json()['budget_reservation']['state'] == 'released'
    request = ExecutionRequest.objects.get(id=execution['id'])
    management.grant.refresh_from_db()
    assert request.state == 'cancelled' and request.reservation_state == 'released'
    assert management.grant.reserved_cpu == 0 and management.grant.spent_cpu == 0
    assert LedgerEntry.objects.count() == ledger_count + 1
    assert not Attempt.objects.filter(job=job).exists()
    assert metadata_usage(management.project) == before
    assert IdempotencyRecord.objects.count() == 1


def test_collection_recovery_rejects_active_native_job_and_removed_cluster(management):
    job, attempt = admitted(management)
    browser = human(management.owner)
    response = form_post(browser, action_url(job, attempt, 'collect'), {'idempotency_key': str(uuid4())})
    assert response.status_code == 409 and b'finished native job' in response.content
    management.connections[0].archived = True
    management.connections[0].save(update_fields=['archived'])
    response = form_post(browser, action_url(job, attempt, 'collect'), {'idempotency_key': str(uuid4())})
    assert response.status_code == 409 and b'unavailable for output recovery' in response.content


def test_disabled_cluster_still_allows_recovery_of_existing_finished_work(management):
    job, attempt = admitted(management, outputs=['result.txt'])
    Attempt.objects.filter(id=attempt.id).update(state='succeeded', native_identity={'job_id': '123'},
        observation={'terminal': True, 'state': 'succeeded'}, collection_state='failed')
    management.connections[0].enabled = False
    management.connections[0].save(update_fields=['enabled'])
    browser = human(management.owner)
    assert b'Retry output collection' in browser.get(f'/review/{job.id}/').content
    assert form_post(browser, action_url(job, attempt, 'collect'), {'idempotency_key': str(uuid4())}).status_code == 302
    attempt.refresh_from_db()
    assert attempt.collection_pending and Attempt.objects.filter(job=job).count() == 1
    management.worker.revoked = True
    management.worker.save(update_fields=['revoked'])
    assert form_post(browser, action_url(job, attempt, 'collect'), {'idempotency_key': str(uuid4())}).status_code == 409


def test_declared_results_download_requires_owner_and_verifies_bytes(management):
    job, attempt = admitted(management, outputs=['result.txt'])
    blob = store_blob(management.project, b'valid scientific result\n')
    artifact = Artifact.objects.create(attempt=attempt, path='result.txt', blob=blob)
    url = f'/review/{job.id}/results/{artifact.id}/'
    browser = human(management.owner)
    review = browser.get(f'/review/{job.id}/')
    assert url.encode() in review.content and blob.sha256.encode() in review.content
    response = browser.get(url)
    assert response.status_code == 200
    assert b''.join(response.streaming_content) == b'valid scientific result\n'
    assert response['Cache-Control'] == 'no-store, private'
    assert response['Content-Disposition'] == 'attachment; filename="result.txt"'
    assert human(management.outsider).get(url).status_code == 403
    assert Client().get(url, HTTP_AUTHORIZATION='Bearer ' + management.token).status_code == 302
    other = prepared(management)
    assert browser.get(f'/review/{other.id}/results/{artifact.id}/').status_code == 404
    hidden = Artifact.objects.create(attempt=attempt, path='not-declared.txt', blob=blob)
    assert browser.get(f'/review/{job.id}/results/{hidden.id}/').status_code == 404
    blob_path(blob).write_bytes(b'corrupt scientific result\n')
    assert browser.get(url).status_code == 409
