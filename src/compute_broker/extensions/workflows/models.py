import uuid

from django.db import models
from django.utils import timezone


class WorkflowRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("broker.Project", on_delete=models.PROTECT)
    name = models.CharField(max_length=80)
    document = models.JSONField()
    sha256 = models.CharField(max_length=64)
    connections = models.ManyToManyField("broker.Connection")
    created_at = models.DateTimeField(default=timezone.now)


class WorkflowRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.ForeignKey(WorkflowRevision, on_delete=models.PROTECT, related_name="runs")
    credential = models.ForeignKey("broker.ProjectCredential", on_delete=models.PROTECT)
    state = models.CharField(max_length=30, default="queued")
    cancel_requested = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    next_check_at = models.DateTimeField(default=timezone.now)


class WorkflowNodeRun(models.Model):
    run = models.ForeignKey(WorkflowRun, on_delete=models.PROTECT, related_name="nodes")
    key = models.CharField(max_length=80)
    state = models.CharField(max_length=30, default="blocked_inputs")
    job = models.OneToOneField("broker.Job", null=True, on_delete=models.PROTECT)
    execution_request = models.OneToOneField("broker.ExecutionRequest", null=True, on_delete=models.PROTECT)
    resolved_inputs = models.JSONField(default=list)
    error_code = models.CharField(max_length=80, blank=True)
    next_check_at = models.DateTimeField(default=timezone.now)
    claim_expires_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "key"], name="workflow_run_node_unique")]
