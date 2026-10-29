"""Inline and streamed publication identify the quota that actually blocks them."""

import hashlib

import pytest

from compute_relay.control import storage
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import StoragePool, UploadReservation
from test_support.unit_test_control import broker


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("resource,scope", [
    ("stored_bytes", "project"), ("stored_bytes", "installation"),
    ("stored_objects", "project"), ("incomplete_transfers", "project"),
])
def test_publication_identifies_exhausted_capacity(broker, settings, resource, scope, streamed):
    if resource == "stored_bytes":
        setattr(settings, "PROJECT_STORAGE_LIMIT" if scope == "project" else "GLOBAL_STORAGE_LIMIT", 1)
    elif resource == "stored_objects":
        storage.store_blob(broker.project, b"first")
        settings.MAX_OBJECTS = 1
    else:
        for index in range(storage.MAX_PENDING_UPLOADS):
            storage.begin_transfer(broker.project, hashlib.sha256(str(index).encode()).hexdigest(), 8, {})
    before = UploadReservation.objects.filter(state="reserved").count()
    content = b"new-data"
    with pytest.raises(BrokerError) as raised:
        if streamed:
            storage.begin_transfer(broker.project, hashlib.sha256(content).hexdigest(), len(content), {})
        else:
            storage.store_blob(broker.project, content)
    error = raised.value
    assert error.code == "STORAGE_QUOTA" and error.status == 409
    assert error.details["scope"] == scope and error.details["resource"] == resource
    assert error.details["remaining"] >= 0
    assert error.details["used"] + error.details["requested"] > error.details["limit"]
    assert UploadReservation.objects.filter(state="reserved").count() == before
    pool = StoragePool.objects.filter(id=1).first()
    assert (pool.reserved if pool else 0) == before * 8
