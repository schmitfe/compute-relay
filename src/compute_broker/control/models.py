import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.utils import timezone


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    local_login_allowed = models.BooleanField(default=True)


class WorkerIdentity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80)
    token_hash = models.CharField(max_length=64, unique=True)
    revoked = models.BooleanField(default=False)
    owner = models.ForeignKey("User", null=True, blank=True, on_delete=models.PROTECT)
    profiles = models.JSONField(default=list)
    supported_extensions = models.JSONField(default=list)
    last_seen_at = models.DateTimeField(null=True)
    enrollment_nonce = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    connector_features = models.JSONField(default=list)

    def __str__(self):
        return self.name


class Connection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.PROTECT)
    worker = models.ForeignKey(WorkerIdentity, on_delete=models.PROTECT)
    name = models.CharField(max_length=80)
    backend = models.CharField(max_length=20)
    configuration = models.JSONField()
    revision_sha256 = models.CharField(max_length=64)
    enabled = models.BooleanField(default=True)
    archived = models.BooleanField(default=False)
    setup_complete = models.BooleanField(default=True)
    configuration_confirmed = models.BooleanField(default=True)
    auth_state = models.CharField(max_length=30, default="ready")
    discovery_snapshot = models.JSONField(default=dict)
    discovery_state = models.CharField(max_length=30, default="not_started")
    discovery_error = models.CharField(max_length=80, blank=True)
    discovery_generation = models.PositiveIntegerField(default=0)
    discovery_lease_id = models.UUIDField(null=True)
    discovery_lease_expires_at = models.DateTimeField(null=True)

    def __str__(self):
        return self.name


class ClusterRegistration(models.Model):
    """Human setup intent; no executable connection exists until explicitly bound."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.PROTECT)
    name = models.CharField(max_length=80)
    backend = models.CharField(max_length=20, default="slurm")
    host = models.CharField(max_length=253)
    remote_user = models.CharField(max_length=80, blank=True)
    port = models.PositiveIntegerField(default=22)
    requested_root = models.CharField(max_length=240, blank=True)
    connector_settings = models.JSONField(default=dict)
    archived = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)
    connection = models.OneToOneField(Connection, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="registration")
    created_at = models.DateTimeField(default=timezone.now)


class ConnectorEnrollment(models.Model):
    """One-use human-authorized device pairing; never stores a bearer value."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    registration = models.ForeignKey(ClusterRegistration, on_delete=models.PROTECT)
    token_hash = models.CharField(max_length=64, unique=True)
    setup_revision = models.PositiveIntegerField()
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True)
    worker = models.ForeignKey(WorkerIdentity, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)


class ConnectorTask(models.Model):
    """Owner-only setup work, consumed by that user's personal connector."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT)
    owner = models.ForeignKey(User, on_delete=models.PROTECT)
    kind = models.CharField(max_length=20)
    state = models.CharField(max_length=20, default="queued")
    request = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    connection_revision = models.CharField(max_length=64)
    generation = models.PositiveIntegerField(default=0)
    lease_id = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["connection", "state", "created_at"], name="connector_task_pending")]


class Environment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80)
    configuration = models.JSONField()
    revision_sha256 = models.CharField(max_length=64)
    owner = models.ForeignKey("User", null=True, blank=True, on_delete=models.PROTECT)
    connection = models.ForeignKey(Connection, null=True, blank=True, on_delete=models.PROTECT)

    def __str__(self):
        return self.name


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    owner = models.ForeignKey(User, on_delete=models.PROTECT)
    connections = models.ManyToManyField(Connection)
    environments = models.ManyToManyField(Environment)
    paused = models.BooleanField(default=False)
    storage_used = models.BigIntegerField(default=0)
    storage_reserved = models.BigIntegerField(default=0)
    object_count = models.PositiveIntegerField(default=0)


class Membership(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    roles = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "user"], name="membership_identity")]


class ProjectCredential(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    issued_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="issued_project_credentials")
    name = models.CharField(max_length=80)
    token_hash = models.CharField(max_length=64, unique=True)
    prefix = models.CharField(max_length=16)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    revoked = models.BooleanField(default=False)
    allowed_connection_ids = models.JSONField(null=True, blank=True, default=None)
    execution_mode = models.CharField(max_length=20, default="inherit", choices=[
        ("inherit", "Follow project policy"), ("approval_required", "Require human confirmation"),
        ("automatic", "Automatic within project policy")])
    created_at = models.DateTimeField(default=timezone.now)
    replaced_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)


class Grant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.OneToOneField(Project, on_delete=models.PROTECT)
    policy = models.JSONField()
    spent_cpu = models.BigIntegerField(default=0)
    spent_gpu = models.BigIntegerField(default=0)
    reserved_cpu = models.BigIntegerField(default=0)
    reserved_gpu = models.BigIntegerField(default=0)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(spent_cpu__gte=0, spent_gpu__gte=0, reserved_cpu__gte=0, reserved_gpu__gte=0), name="nonnegative_balances")]


class StoragePool(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    used = models.BigIntegerField(default=0)
    reserved = models.BigIntegerField(default=0)


class UploadReservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    size_bytes = models.PositiveIntegerField()
    state = models.CharField(max_length=12, default="reserved")
    created_at = models.DateTimeField(default=timezone.now)


class Blob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "sha256"], name="project_blob_digest")]


class JobQuerySet(models.QuerySet):
    def with_summary(self):
        latest = ExecutionRequest.objects.filter(job_id=models.OuterRef("pk")).order_by("-created_at", "-id")
        return self.select_related("project__owner", "credential").annotate(
            _latest_execution_state=models.Subquery(latest.values("state")[:1]),
            _latest_attempt_state=models.Subquery(latest.values("attempt__state")[:1]),
        )


class Job(models.Model):
    objects = JobQuerySet.as_manager()
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    credential = models.ForeignKey(ProjectCredential, null=True, on_delete=models.PROTECT)
    previous = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    request = models.JSONField()
    extension_name = models.CharField(max_length=64, blank=True, default="")
    state = models.CharField(max_length=30, default="draft")
    manifest = models.JSONField(null=True)
    manifest_sha256 = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    @property
    def name(self):
        return self.request.get("name", str(self.id))

    @property
    def display_state(self):
        if hasattr(self, "_latest_execution_state"):
            return self._latest_attempt_state or self._latest_execution_state or self.state
        execution = self.execution_requests.order_by("-created_at", "-id").first()
        if execution:
            attempt = Attempt.objects.filter(execution_request=execution).first()
            return attempt.state if attempt else execution.state
        return self.state

    @property
    def display_state_label(self):
        state = self.display_state
        return {"succeeded": "Completed", "rejected": "Rejected", "awaiting_approval": "Awaiting approval",
                "settlement_pending": "Awaiting accounting", "submission_uncertain": "Submission uncertain"}.get(
                    state, state.replace("_", " ").capitalize())


class JobFile(models.Model):
    job = models.ForeignKey(Job, related_name="files", on_delete=models.PROTECT)
    path = models.CharField(max_length=240)
    blob = models.ForeignKey(Blob, on_delete=models.PROTECT)
    executable = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "path"], name="job_file_path")]


class ExecutionRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, related_name="execution_requests", on_delete=models.PROTECT)
    credential = models.ForeignKey(ProjectCredential, null=True, on_delete=models.PROTECT)
    state = models.CharField(max_length=30, default="awaiting_approval")
    created_at = models.DateTimeField(default=timezone.now)


class ApprovalDecision(models.Model):
    execution_request = models.OneToOneField(ExecutionRequest, on_delete=models.PROTECT)
    approver = models.ForeignKey(User, on_delete=models.PROTECT)
    approved = models.BooleanField()
    manifest_sha256 = models.CharField(max_length=64)
    comment = models.TextField(blank=True)
    consumed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now)


class Attempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    execution_request = models.OneToOneField(ExecutionRequest, related_name="attempt", on_delete=models.PROTECT)
    job = models.ForeignKey(Job, related_name="attempts", on_delete=models.PROTECT)
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT)
    grant = models.ForeignKey(Grant, on_delete=models.PROTECT)
    state = models.CharField(max_length=30, default="authorized")
    native_identity = models.JSONField(null=True)
    observation = models.JSONField(default=dict)
    reservation_cpu = models.BigIntegerField()
    reservation_gpu = models.BigIntegerField()
    accounting_state = models.CharField(max_length=30, default="reserved")
    operation = models.CharField(max_length=20, default="submit")
    generation = models.PositiveIntegerField(default=0)
    lease_id = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    dispatch_started = models.BooleanField(default=False)
    cancel_requested = models.BooleanField(default=False)
    next_poll_at = models.DateTimeField(default=timezone.now)
    stdout = models.TextField(blank=True)
    stderr = models.TextField(blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(default=timezone.now)


class LedgerEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant = models.ForeignKey(Grant, related_name="entries", on_delete=models.PROTECT)
    attempt = models.ForeignKey(Attempt, on_delete=models.PROTECT)
    action = models.CharField(max_length=20)
    cpu = models.BigIntegerField()
    gpu = models.BigIntegerField()
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["attempt", "action"], name="ledger_action_once")]


class Artifact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attempt = models.ForeignKey(Attempt, related_name="artifacts", on_delete=models.PROTECT)
    path = models.CharField(max_length=240)
    blob = models.ForeignKey(Blob, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["attempt", "path"], name="artifact_path_once")]


class IdempotencyRecord(models.Model):
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    operation = models.CharField(max_length=150)
    key = models.CharField(max_length=128)
    request_sha256 = models.CharField(max_length=64)
    response = models.JSONField()
    status = models.PositiveIntegerField(default=200)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "operation", "key"], name="idempotent_operation")]


class AuditEvent(models.Model):
    project = models.ForeignKey(Project, null=True, on_delete=models.PROTECT)
    actor = models.CharField(max_length=100)
    action = models.CharField(max_length=100)
    object_id = models.CharField(max_length=80)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)


class WorkerReport(models.Model):
    lease_id = models.UUIDField(primary_key=True)
    attempt = models.ForeignKey(Attempt, on_delete=models.PROTECT)
    generation = models.PositiveIntegerField()
    payload_sha256 = models.CharField(max_length=64)
    response = models.JSONField()
    extension_data = models.JSONField(default=dict)


# Identity models are part of the core app's migration graph, while their
# provider and directory code remains an optional module.
from .identity.models import AuthenticationIdentity  # noqa: E402,F401
