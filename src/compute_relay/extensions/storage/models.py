"""Remote storage planning records, distinct from the broker's Blob storage pool."""

import uuid

from django.db import models
from django.utils import timezone

from .planning import default_health_policy


class StorageCapacityPool(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey("broker.User", on_delete=models.PROTECT)
    connection = models.ForeignKey("broker.Connection", on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    probe_root = models.CharField(max_length=4096)
    expected_filesystem_id = models.CharField(max_length=128, null=True, blank=True)
    health_policy = models.JSONField(default=default_health_policy)
    observation = models.JSONField(default=dict)
    health_state = models.CharField(max_length=20, default="unknown")
    recovery_since = models.DateTimeField(null=True)
    enabled = models.BooleanField(default=True)
    probe_enabled = models.BooleanField(default=False)
    next_probe_at = models.DateTimeField(default=timezone.now)
    probe_generation = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.name


class StorageTarget(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey("broker.User", on_delete=models.PROTECT)
    project = models.ForeignKey("broker.Project", on_delete=models.PROTECT)
    connection = models.ForeignKey("broker.Connection", on_delete=models.PROTECT)
    pool = models.ForeignKey(StorageCapacityPool, on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    root_alias = models.CharField(max_length=80)
    root = models.CharField(max_length=4096)
    roles = models.JSONField(default=list)
    capabilities = models.JSONField(default=dict)
    profile_revision_sha256 = models.CharField(max_length=64)
    connection_revision_sha256 = models.CharField(max_length=64)
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "connection", "root_alias", "profile_revision_sha256", "connection_revision_sha256"], name="storage_project_target_once")]

    def __str__(self):
        return self.name


class StorageTargetRegistration(models.Model):
    """Human-selected root and its exact, owner-requested connector operation."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey("broker.User", on_delete=models.PROTECT)
    project = models.ForeignKey("broker.Project", on_delete=models.PROTECT)
    connection = models.ForeignKey("broker.Connection", on_delete=models.PROTECT)
    task = models.OneToOneField("broker.ConnectorTask", on_delete=models.PROTECT)
    target = models.OneToOneField(StorageTarget, on_delete=models.PROTECT, null=True,
                                related_name="personal_registration")
    name = models.CharField(max_length=100)
    root = models.CharField(max_length=240)
    roles = models.JSONField(default=list)
    profile_revision_sha256 = models.CharField(max_length=64)
    connection_revision_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)


class ProjectStorageBudget(models.Model):
    project = models.OneToOneField("broker.Project", primary_key=True, on_delete=models.PROTECT)
    bytes_budget = models.BigIntegerField(null=True, blank=True)
    inodes_budget = models.BigIntegerField(null=True, blank=True)
    retained_bytes_estimate = models.BigIntegerField(default=0)
    retained_inodes_estimate = models.BigIntegerField(default=0)
    estimate_updated_at = models.DateTimeField(null=True)
    estimate_source = models.CharField(max_length=30, default="not_estimated")


class ProjectStorageBinding(models.Model):
    project = models.ForeignKey("broker.Project", on_delete=models.PROTECT)
    connection = models.ForeignKey("broker.Connection", on_delete=models.PROTECT)
    work = models.ForeignKey(StorageTarget, null=True, blank=True, on_delete=models.PROTECT, related_name="work_bindings")
    environments = models.ForeignKey(StorageTarget, null=True, blank=True, on_delete=models.PROTECT, related_name="environment_bindings")
    data = models.ForeignKey(StorageTarget, null=True, blank=True, on_delete=models.PROTECT, related_name="data_bindings")
    results = models.ForeignKey(StorageTarget, null=True, blank=True, on_delete=models.PROTECT, related_name="result_bindings")
    scratch = models.ForeignKey(StorageTarget, null=True, blank=True, on_delete=models.PROTECT, related_name="scratch_bindings")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "connection"], name="project_storage_connection_once")]


class StorageEstimateReservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("broker.Project", on_delete=models.PROTECT)
    target = models.ForeignKey(StorageTarget, on_delete=models.PROTECT)
    reference = models.UUIDField(unique=True)
    bytes_estimate = models.BigIntegerField()
    inodes_estimate = models.BigIntegerField()
    state = models.CharField(max_length=20, default="held")
    created_at = models.DateTimeField(default=timezone.now)
    released_at = models.DateTimeField(null=True)


class StorageObservationLease(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pool = models.ForeignKey(StorageCapacityPool, on_delete=models.PROTECT)
    target = models.ForeignKey(StorageTarget, on_delete=models.PROTECT)
    generation = models.PositiveIntegerField()
    profile_revision_sha256 = models.CharField(max_length=64)
    connection_revision_sha256 = models.CharField(max_length=64)
    state = models.CharField(max_length=20, default="leased")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    reported_at = models.DateTimeField(null=True)
    report_sha256 = models.CharField(max_length=64, blank=True)
    response = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["pool", "generation"], name="storage_probe_generation_once"),
            models.UniqueConstraint(fields=["pool"], condition=models.Q(state="leased"), name="storage_probe_pool_active_once"),
        ]
