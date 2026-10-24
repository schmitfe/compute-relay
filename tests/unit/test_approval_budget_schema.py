"""Approval holds have durable independent anchors and database constraints."""

from types import SimpleNamespace

import pytest
from django.db import IntegrityError, transaction

from compute_relay.control.models import (Attempt, Connection, ExecutionRequest, Grant, Job, LedgerEntry,
    Project, User, WorkerIdentity)


@pytest.fixture
def approval_records(db):
    owner = User.objects.create_user(username="synthetic-approval-journal")
    project = Project.objects.create(owner=owner, name="Synthetic offline journal", paused=True)
    grant = Grant.objects.create(project=project, policy={})
    worker = WorkerIdentity.objects.create(owner=owner, name="Synthetic journal worker", token_hash="a" * 64)
    connection = Connection.objects.create(owner=owner, worker=worker, name="Synthetic journal cluster", backend="fake",
        configuration={}, revision_sha256="b" * 64, discovery_state="ready")
    job = Job.objects.create(project=project, request={}, manifest={})
    execution = ExecutionRequest.objects.create(job=job, state="rejected")
    return SimpleNamespace(owner=owner, project=project, grant=grant, worker=worker, connection=connection,
        job=job, execution=execution)


@pytest.mark.parametrize("field,value", [("reservation_cpu", -1), ("reservation_gpu", -1), ("reservation_state", "unknown")])
def test_execution_hold_constraints_are_enforced_by_database(approval_records, field, value):
    with pytest.raises(IntegrityError), transaction.atomic():
        ExecutionRequest.objects.filter(id=approval_records.execution.id).update(**{field: value})


def test_ledger_requires_exactly_one_anchor_and_enforces_each_action_once(approval_records):
    context = approval_records
    attempt = Attempt.objects.create(job=context.job, execution_request=context.execution, connection=context.connection,
        grant=context.grant, reservation_cpu=60, reservation_gpu=0)
    for anchors in ({}, {"attempt": attempt, "execution_request": context.execution}):
        with pytest.raises(IntegrityError), transaction.atomic():
            LedgerEntry.objects.create(grant=context.grant, action="hold", cpu=60, gpu=0, **anchors)
    LedgerEntry.objects.create(grant=context.grant, execution_request=context.execution, action="hold", cpu=60, gpu=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        LedgerEntry.objects.create(grant=context.grant, execution_request=context.execution, action="hold", cpu=60, gpu=0)
    LedgerEntry.objects.create(grant=context.grant, attempt=attempt, action="reserve", cpu=60, gpu=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        LedgerEntry.objects.create(grant=context.grant, attempt=attempt, action="reserve", cpu=60, gpu=0)
