"""One-time human key receipts obey quotas without retaining plaintext keys."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from compute_relay.control.errors import BrokerError
from compute_relay.control.management import issue_key, rotate_key
from compute_relay.control.metadata import compact_idempotency, json_size, metadata_usage
from compute_relay.control.models import IdempotencyRecord, MetadataUsage, ProjectCredential
from test_support.unit_test_control import broker


def data():
    return {"name": "one-time", "expires_at": timezone.now() + timedelta(days=1),
        "scopes": ["jobs:read"], "execution_mode": "inherit", "cluster_access": "inherit",
        "allowed_connections": [], "submission_id": uuid4()}


def test_key_receipt_charges_response_bytes_and_never_reveals_secret_again(broker, settings):
    request = data()
    credential, secret = issue_key(broker.project, broker.owner, request)
    receipt = IdempotencyRecord.objects.get()
    charge = MetadataUsage.objects.get(kind="idempotency")
    assert charge.size_bytes == 512 + json_size(receipt.response)
    assert receipt.response == {"credential_id": str(credential.id)}
    assert secret not in json.dumps(receipt.response)
    assert credential.token_hash != secret
    settings.MAX_PROJECT_IDEMPOTENCY_KEYS = 0
    compact_idempotency(before=timezone.now() + timedelta(seconds=1))
    with pytest.raises(BrokerError) as error:
        issue_key(broker.project, broker.owner, request)
    assert error.value.code == "SECRET_ALREADY_SHOWN"
    assert ProjectCredential.objects.count() == 2 and IdempotencyRecord.objects.count() == 1


def test_receipt_byte_quota_rejection_does_not_create_a_key(broker, settings):
    settings.PROJECT_METADATA_LIMIT = 512
    with pytest.raises(BrokerError) as error:
        issue_key(broker.project, broker.owner, data())
    assert error.value.code == "METADATA_QUOTA"
    assert ProjectCredential.objects.count() == 1
    assert not IdempotencyRecord.objects.exists() and not MetadataUsage.objects.exists()


def test_rotation_quota_failure_keeps_original_key_and_creates_no_replacement(broker, settings):
    issue_key(broker.project, broker.owner, data())
    settings.MAX_PROJECT_IDEMPOTENCY_KEYS = 1
    before = metadata_usage(broker.project)
    with pytest.raises(BrokerError) as error:
        rotate_key(broker.project, broker.credential, broker.owner, uuid4())
    assert error.value.code == "METADATA_QUOTA"
    broker.credential.refresh_from_db()
    assert not broker.credential.revoked and broker.credential.replaced_by_id is None
    assert ProjectCredential.objects.count() == 2 and metadata_usage(broker.project) == before


def test_successful_rotation_adds_exactly_one_receipt_charge(broker):
    replacement, secret = rotate_key(broker.project, broker.credential, broker.owner, uuid4())
    broker.credential.refresh_from_db()
    assert broker.credential.revoked and broker.credential.replaced_by_id == replacement.id
    receipt = IdempotencyRecord.objects.get()
    assert MetadataUsage.objects.get(kind="idempotency").size_bytes == 512 + json_size(receipt.response)
    assert secret not in json.dumps(receipt.response)
