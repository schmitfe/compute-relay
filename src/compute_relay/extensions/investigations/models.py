import uuid
from django.db import models
from django.utils import timezone


class Investigation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("broker.Project", on_delete=models.PROTECT)
    connections = models.ManyToManyField("broker.Connection", through="InvestigationConnection")
    name = models.CharField(max_length=80)
    plan = models.JSONField()
    state = models.CharField(max_length=20, default="open")
    conclusion = models.CharField(max_length=30, blank=True)
    report = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True)


class InvestigationConnection(models.Model):
    investigation = models.ForeignKey(Investigation, related_name="connection_scope", on_delete=models.CASCADE)
    connection = models.ForeignKey("broker.Connection", on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["investigation", "connection"], name="investigation_connection_scope")]


class InvestigationJob(models.Model):
    investigation = models.ForeignKey(Investigation, related_name="members", on_delete=models.PROTECT)
    job = models.OneToOneField("broker.Job", related_name="investigation_membership", on_delete=models.PROTECT)
    role = models.CharField(max_length=20, default="core")
    created_at = models.DateTimeField(default=timezone.now)
