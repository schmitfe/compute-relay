"""Freshness-labelled scheduler observations, independent of admission limits."""

from datetime import timedelta
from copy import deepcopy
import math
from uuid import uuid4

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .auth import connection_allowed, worker_identity
from .errors import BrokerError
from .models import Attempt, Connection, WorkerIdentity
from .views import body, boundary, scope


def public_availability(connection):
    snapshot = deepcopy(connection.availability_snapshot)
    if not isinstance(snapshot, dict):
        snapshot = {}
    rows = snapshot.get("partitions", [])
    snapshot["partitions"] = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    # Stored observations can outlive the reporting connector process. Normalize
    # existing ratios at the public boundary as well as in new observations.
    def ratio(value):
        if isinstance(value, float):
            return format(value, ".6f").rstrip("0").rstrip(".") if math.isfinite(value) and value >= 0 else None
        return str(value) if type(value) is int else value
    for row in snapshot.get("partitions", []):
        pressure = row.get("queue_pressure")
        if isinstance(pressure, dict):
            pressure["ratio_encoding"] = "decimal_string"
            pressure["pending_jobs_per_schedulable_node"] = ratio(pressure.get("pending_jobs_per_schedulable_node"))
            devices = pressure.get("pending_gpu_jobs_per_schedulable_device", {})
            pressure["pending_gpu_jobs_per_schedulable_device"] = {kind: ratio(value) for kind, value in
                (devices.items() if isinstance(devices, dict) else [])}
    try:
        value = snapshot.get("observed_at", "")
        observed = parse_datetime(value) if isinstance(value, str) else None
    except ValueError:
        observed = None
    age = max(0, int((timezone.now() - observed).total_seconds())) if observed and timezone.is_aware(observed) else None
    partitions = connection.configuration.get("site_profile", {}).get("partitions", [])
    if partitions:
        snapshot["partitions"] = [row for row in snapshot.get("partitions", []) if row.get("name") in partitions]
    current = snapshot.get("connection_revision_sha256") == connection.revision_sha256
    statuses = [snapshot.get(key) == "available" for key in ("nodes_status", "queue_status")]
    status = ("ready" if all(statuses) else "partial" if any(statuses) else "unknown") if current else "unknown"
    return {**snapshot, "status": status, "age_seconds": age,
        "stale": age is None or age > 120 or not current,
        "refresh_pending": bool(connection.availability_requested_at), "refresh_tool": "get_compute_availability",
        "connector_upgrade_required": "availability_v1" not in connection.worker.execution_features,
        "admission_limits_are_capacity": False, "placement_guaranteed": False}


@boundary("GET", "POST")
def availability(request, project_id):
    credential, project = scope(request, "jobs:read", project_id=project_id)
    data = body(request) if request.method == "POST" else {}
    if set(data) - {"connection_ids"}:
        raise ValueError("Invalid availability options")
    connections = [item for item in project.connections.select_related("worker") if connection_allowed(credential, item.id)]
    selected = data.get("connection_ids")
    if selected is not None:
        if not isinstance(selected, list) or len(selected) > 100 or any(not isinstance(value, str) for value in selected):
            raise ValueError("Invalid target list")
        if set(selected) - {str(item.id) for item in connections}:
            raise BrokerError("CONNECTION_DENIED", "A selected cluster is outside this key's scope.", 403)
        connections = [item for item in connections if str(item.id) in selected]
    if request.method == "POST":
        now = timezone.now()
        for connection in connections:
            if connection.backend == "slurm" and "availability_v1" in connection.worker.execution_features:
                changed = Connection.objects.filter(id=connection.id, availability_requested_at__isnull=True).filter(
                    Q(availability_checked_at__isnull=True) | Q(availability_checked_at__lte=now - timedelta(seconds=30))).update(
                        availability_requested_at=now)
                if changed:
                    connection.availability_requested_at = now
    return {"connections": [{"id": str(item.id), "name": item.name, "availability": public_availability(item)} for item in connections],
        "refresh_interval_seconds": 30, "costs": {"status": "not_configured"}, "automatic_placement": False}


def lease_availability(worker, *, exclude_attempt_ids=()):
    if "availability_v1" not in worker.execution_features:
        return None
    now = timezone.now()
    with transaction.atomic():
        WorkerIdentity.objects.select_for_update().get(id=worker.id, revoked=False)
        # Leave time for normal job dispatch/monitoring between observations,
        # even when many clusters are refreshed together. Never queue a backlog
        # of availability calls on one connector or delay due cancellation and
        # uncertain-submission recovery for a capacity refresh.
        if Connection.objects.filter(worker=worker).filter(
                Q(availability_checked_at__gt=now - timedelta(seconds=5)) | Q(availability_lease_expires_at__gt=now)).exists():
            return None
        if Attempt.objects.filter(connection__worker=worker, next_poll_at__lte=now).exclude(id__in=exclude_attempt_ids).filter(
                Q(cancel_requested=True, native_identity__isnull=False) | Q(dispatch_started=True, native_identity__isnull=True)).filter(
                Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)).filter(
                ~Q(accounting_state__in=["settled", "released"]) | Q(collection_pending=True)).exists():
            return None
        connection = Connection.objects.select_for_update().filter(worker=worker, enabled=True, archived=False,
            setup_complete=True, availability_requested_at__isnull=False).filter(
                Q(availability_lease_expires_at__isnull=True) | Q(availability_lease_expires_at__lte=now)).order_by("availability_requested_at", "id").first()
        if not connection:
            return None
        connection.availability_generation += 1
        connection.availability_lease_id = uuid4()
        connection.availability_lease_expires_at = now + timedelta(seconds=60)
        connection.save(update_fields=["availability_generation", "availability_lease_id", "availability_lease_expires_at"])
        return {"lease": {"operation": "availability", "connection_id": str(connection.id), "id": str(connection.availability_lease_id),
            "generation": connection.availability_generation, "configuration": connection.configuration,
            "connection_revision_sha256": connection.revision_sha256}}


@boundary("POST")
def report(request, connection_id):
    import json
    worker = worker_identity(request)
    data = body(request, maximum=131072)
    with transaction.atomic():
        connection = Connection.objects.select_for_update().get(id=connection_id, worker=worker, archived=False)
        if (str(connection.availability_lease_id) != data.get("lease_id") or connection.availability_generation != data.get("generation")
                or not connection.availability_lease_expires_at or connection.availability_lease_expires_at <= timezone.now()
                or data.get("connection_revision_sha256") != connection.revision_sha256):
            raise BrokerError("STALE_LEASE", "Availability observation lease is stale.", 409)
        snapshot = data.get("snapshot")
        if (not isinstance(snapshot, dict) or len(json.dumps(snapshot)) > 65536
                or not isinstance(snapshot.get("partitions"), list)
                or any(not isinstance(row, dict) for row in snapshot["partitions"])):
            raise ValueError("Invalid availability snapshot")
        connection.availability_snapshot = {**snapshot, "observed_at": timezone.now().isoformat(),
            "connection_revision_sha256": connection.revision_sha256}
        connection.availability_checked_at = timezone.now()
        connection.availability_requested_at = connection.availability_lease_expires_at = None
        connection.save(update_fields=["availability_snapshot", "availability_checked_at", "availability_requested_at", "availability_lease_expires_at"])
    return {"accepted": True}
