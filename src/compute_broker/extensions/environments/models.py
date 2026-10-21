"""Project-owned environment recipes and scheduled build provenance."""

import uuid

from django.db import models
from django.utils import timezone


class EnvironmentBuildPolicy(models.Model):
    project = models.OneToOneField("broker.Project", on_delete=models.PROTECT, related_name="environment_build_policy")
    configuration = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey("broker.User", null=True, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(default=timezone.now)


class EnvironmentSpec(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("broker.Project", on_delete=models.PROTECT)
    connection = models.ForeignKey("broker.Connection", on_delete=models.PROTECT)
    base_environment = models.ForeignKey("broker.Environment", on_delete=models.PROTECT)
    document = models.JSONField()
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "sha256"], name="environment_spec_project_digest")]


class EnvironmentBuild(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    spec = models.ForeignKey(EnvironmentSpec, on_delete=models.PROTECT, related_name="builds")
    policy = models.ForeignKey(EnvironmentBuildPolicy, on_delete=models.PROTECT)
    policy_revision = models.PositiveIntegerField()
    policy_sha256 = models.CharField(max_length=64)
    credential = models.ForeignKey("broker.ProjectCredential", on_delete=models.PROTECT)
    job = models.OneToOneField("broker.Job", null=True, on_delete=models.PROTECT, related_name="environment_build")
    attempt = models.OneToOneField("broker.Attempt", null=True, on_delete=models.PROTECT, related_name="environment_build")
    environment = models.OneToOneField("broker.Environment", null=True, on_delete=models.PROTECT, related_name="build_provenance")
    state = models.CharField(max_length=32, default="draft")
    receipt = models.JSONField(default=dict)
    error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
