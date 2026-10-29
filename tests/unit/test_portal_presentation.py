"""Human capacity and units never change admission or require filesystem scans."""

from decimal import Decimal
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest

from compute_relay.control.metadata import capacity_rows
from compute_relay.control.models import IdempotencyRecord, MetadataUsage, StoragePool, UploadReservation
from compute_relay.control.templatetags.broker_resource_limits import compute_hours
from test_support.unit_test_management import management, human


def test_human_hours_do_not_round_up_and_keep_exact_integer_hours():
    for seconds in [0, 1, 59, 60, 3599, 3600, 7200, 36000, 2**53 - 1]:
        rendered = compute_hours(seconds)
        assert Decimal(rendered) * 3600 <= seconds
        assert Decimal(seconds) - Decimal(rendered) * 3600 < Decimal("0.0036")
    assert compute_hours(7200) == "2"
    assert compute_hours(36000) == "10"


def test_capacity_shows_pending_claims_and_actual_active_receipts(management, settings, django_assert_num_queries):
    context = management
    settings.MAX_PROJECT_DRAFT_JOBS = 1
    context.project.storage_used = 120
    context.project.storage_reserved = 30
    context.project.object_count = 2
    context.project.save()
    StoragePool.objects.create(id=1, used=180, reserved=30)
    UploadReservation.objects.create(project=context.project, size_bytes=30, binding="synthetic", state="reserved")
    MetadataUsage.objects.create(project=context.project, kind="job", object_id=str(uuid4()), size_bytes=100)
    MetadataUsage.objects.create(project=context.project, kind="retired_job", object_id=str(uuid4()), size_bytes=20)
    MetadataUsage.objects.create(project=context.project, kind="workflow_run", object_id=str(uuid4()), size_bytes=40)
    for index, compacted in enumerate([False, True]):
        IdempotencyRecord.objects.create(project=context.project, operation="fixture", key=str(index),
            request_sha256="a" * 64, response={}, status=200, compacted=compacted)
    with django_assert_num_queries(5):
        rows = {row["label"]: row for row in capacity_rows(context.project)}
    assert rows["Stored and reserved file bytes"]["used"] == 150
    assert rows["Stored and reserved file bytes"]["global_used"] == 210
    assert rows["Stored files and pending transfers"]["used"] == 3
    assert rows["Incomplete transfers"]["used"] == 1
    assert rows["Retained job records"]["used"] == 1
    assert rows["Retained job records"]["remaining"] == 0 and rows["Retained job records"]["full"]
    assert rows["Archived job identities"]["used"] == 1
    assert rows["Active retry receipts"]["used"] == 1
    assert rows["Module definitions and runs"]["used"] == 1
    assert rows["Logical metadata"]["used"] == 160


def test_project_capacity_is_owner_only_and_does_not_mutate_ledger(management):
    context = management
    before = (context.grant.spent_cpu, context.grant.reserved_cpu)
    response = human(context.owner).get(f"/projects/{context.project.id}/")
    assert response.status_code == 200
    assert b"Available hours" in response.content and b"7200 seconds" in response.content
    assert b"Retained job records" in response.content
    assert b'<main id="main-content"' in response.content
    assert b'Skip to content' in response.content
    denied = human(context.outsider).get(f"/projects/{context.project.id}/")
    assert denied.status_code == 403 and b"Retained job records" not in denied.content
    context.grant.refresh_from_db()
    assert (context.grant.spent_cpu, context.grant.reserved_cpu) == before


def test_polling_preserves_only_matching_disclosure_state():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Browser behavior check needs Node")
    script = (Path(__file__).resolve().parents[2] / "src/compute_relay/control/templates/broker/live_updates_script.html").read_text()
    function = script[script.index("  function preserveDisclosures"):script.index("  function mergeExecutions")]
    check = """
const assert = require('node:assert/strict');
const detail = (name, open) => ({name, open, getAttribute() { return this.name; }});
const old = [detail('capacity-details', true), detail('review-accounting', false)];
const incoming = [detail('capacity-details', false), detail('review-accounting', true), detail('unrelated', true)];
const parent = children => ({querySelectorAll(selector) { return selector === 'details[id]' ? children : []; }});
preserveDisclosures(parent(old), parent(incoming));
assert.deepEqual(incoming.map(item => item.open), [true, false, true]);
"""
    subprocess.run([node, "-e", function + check], check=True, timeout=10)
