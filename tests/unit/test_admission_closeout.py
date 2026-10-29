"""Cancelling an unexecuted package frees capacity without deleting its identity."""

from django.test import Client
import pytest

from compute_relay.control import admission, services
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Job
from test_support.unit_test_control import broker, prepared_job


def test_cancelled_draft_releases_outstanding_capacity(broker, settings):
    settings.MAX_PROJECT_OUTSTANDING_JOBS = 1
    job = prepared_job(broker)
    client = Client(HTTP_AUTHORIZATION="Bearer " + broker.token)
    response = client.post(f"/api/v1/jobs/{job.id}/cancel-prepared", data="{}", content_type="application/json")
    assert response.status_code == 200
    assert admission.outstanding(broker.project)["project"]["used"] == 0
    services.create_job(broker.credential, job.request)
    assert Job.objects.count() == 2
    job.refresh_from_db()
    assert job.state == "cancelled"
    with pytest.raises(BrokerError) as raised:
        services.request_execution(job, broker.credential)
    assert raised.value.code == "BACKLOG_LIMIT"
    assert admission.outstanding(broker.project)["project"]["used"] == 1
