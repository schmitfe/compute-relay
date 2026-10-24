"""Current authority records always name their owner and cluster explicitly."""

from django.db import IntegrityError, transaction
import pytest

from compute_relay.control.models import Environment, ProjectCredential, WorkerIdentity
from test_management import management


@pytest.mark.parametrize("model,field,attribute", [
    (WorkerIdentity, "owner", "worker"),
    (Environment, "owner", "environment"),
    (Environment, "connection", "environment"),
    (ProjectCredential, "issued_by", "credential"),
])
def test_ownership_fields_are_required_by_database(management, model, field, attribute):
    record = getattr(management, attribute)
    with pytest.raises(IntegrityError), transaction.atomic():
        model.objects.filter(id=record.id).update(**{field: None})
