"""Recover project work without relying on the agent's conversational memory."""

import base64
import json
from copy import deepcopy
from datetime import timedelta

from django.test import Client
from django.utils import timezone

from compute_relay.control import services
from compute_relay.control.auth import token_hash
from compute_relay.control.models import Attempt, Project, ProjectCredential
from test_support.unit_test_control import broker, budgeted, dispatched, prepared_job, submit_receipt


def api(broker, token=None):
    return Client(HTTP_AUTHORIZATION="Bearer " + (token or broker.token))


def test_prepared_work_survives_new_client_is_project_scoped_and_can_be_dismissed(broker):
    first, second = prepared_job(broker), prepared_job(broker)
    response = api(broker).get(f"/api/v1/projects/{broker.project.id}/jobs?limit=1").json()
    assert response["jobs"][0]["id"] == str(second.id) and response["next_cursor"]
    more = api(broker).get(f"/api/v1/projects/{broker.project.id}/jobs", {"cursor": response["next_cursor"]}).json()
    assert [item["id"] for item in more["jobs"]] == [str(first.id)]
    assert api(broker).get("/api/v1/context").json()["work_summary"]["unfinished"] == 2
    assert api(broker).post(f"/api/v1/jobs/{first.id}/cancel-prepared", "{}", content_type="application/json").status_code == 200
    assert api(broker).get("/api/v1/context").json()["work_summary"]["unfinished"] == 1
    foreign = Project.objects.create(owner=broker.owner, name="foreign")
    assert api(broker).get(f"/api/v1/projects/{foreign.id}/jobs").status_code == 404
    assert not Attempt.objects.exists()


def test_unread_results_distinguish_listing_bytes_and_acknowledgement_per_key(broker):
    budgeted(broker)
    job = prepared_job(broker, outputs=["answer.txt"])
    attempt, first = dispatched(broker, job)
    native, lease = submit_receipt(broker, attempt, first)
    services.report(broker.worker, attempt.id, {"lease_id": lease["id"], "generation": lease["generation"],
        "outcome": "observed", "native_identity": native, "collection_state": "complete",
        "artifacts": {"answer.txt": base64.b64encode(b"answer").decode()},
        "observation": {"state": "completed", "terminal": True, "accounting": {"final": True, "cpu_core_seconds": 1, "gpu_device_seconds": 0}}})
    endpoint = f"/api/v1/projects/{broker.project.id}/jobs?state=unread_results"
    assert len(api(broker).get(endpoint).json()["jobs"]) == 1
    inventory = api(broker).get(f"/api/v1/attempts/{attempt.id}/artifacts").json()
    listed = api(broker).get(endpoint).json()["jobs"][0]["attempts"][0]
    assert listed["inventory_seen"] and not listed["result_bytes_requested"]
    api(broker).get(f"/api/v1/artifacts/{inventory['artifacts'][0]['id']}/content")
    assert api(broker).get(endpoint).json()["jobs"] == []
    assert api(broker).get("/api/v1/context").json()["work_summary"]["results_awaiting_acknowledgement"] == 1
    assert api(broker).post(f"/api/v1/attempts/{attempt.id}/acknowledge-results", "{}", content_type="application/json").json()["scientific_validation"] == "not_asserted"
    assert api(broker).get(f"/api/v1/projects/{broker.project.id}/jobs").json()["jobs"] == []
    other = deepcopy(broker.credential)
    other.id = None;other.token_hash = token_hash("other-project-key")
    other.save()
    assert len(api(broker, "other-project-key").get(endpoint).json()["jobs"]) == 1
    other.allowed_connection_ids = [];other.save(update_fields=["allowed_connection_ids"])
    assert api(broker, "other-project-key").get(endpoint).json()["jobs"] == []


def test_invalid_pagination_does_not_leak_and_admitted_jobs_use_existing_cancel(broker):
    budgeted(broker)
    job = prepared_job(broker)
    attempt, lease = dispatched(broker, job)
    endpoint = f"/api/v1/projects/{broker.project.id}/jobs"
    for query in ("?state=bogus", "?limit=101", "?cursor=invalid", "?cursor=W10="):
        assert api(broker).get(endpoint + query).status_code == 400
    assert api(broker).post(f"/api/v1/jobs/{job.id}/cancel-prepared", "{}", content_type="application/json").status_code == 409
    assert api(broker).post(f"/api/v1/attempts/{attempt.id}/acknowledge-results", "{}", content_type="application/json").status_code == 400
