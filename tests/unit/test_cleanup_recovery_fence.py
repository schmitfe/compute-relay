"""Output recovery cannot race or reverse approved remote deletion."""

import pytest

from compute_relay.control import services
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Attempt
from test_remote_cleanup import completed
from test_support.unit_test_control import broker


@pytest.mark.parametrize("state", ["queued", "running", "complete"])
def test_remote_cleanup_fences_explicit_collection_recovery(broker, state):
    job, attempt = completed(broker)
    Attempt.objects.filter(id=attempt.id).update(remote_cleanup={"state": state})
    with pytest.raises(BrokerError) as raised:
        services.retry_collection(attempt)
    assert raised.value.code == "OUTPUT_CLEANUP_REQUESTED"
    attempt.refresh_from_db()
    assert not attempt.collection_pending and attempt.collection_state == "complete"
