"""Project-scoped advisory metadata; no SSH, file transfer, deletion, or dispatch."""

from __future__ import annotations

from uuid import UUID

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from compute_broker.control.auth import connection_allowed, human_permission
from compute_broker.control.errors import BrokerError
from compute_broker.control.models import Connection, Project
from compute_broker.control.services import audit
from .models import ProjectStorageBinding, ProjectStorageBudget, StorageCapacityPool, StorageEstimateReservation, StorageTarget
from .planning import (ROLES, StoragePlanningError, assess_health, canonical_root, integer,
                       plan_storage, timestamp, total, validate_observation, validate_storage_roots)


def protected_roots(connection):
    """Only a currently published, owned protected profile authorizes a root."""
    worker = connection.worker
    if not connection.enabled or worker.revoked or worker.owner_id != connection.owner_id:
        return []
    config = connection.configuration
    profile = next((item for item in worker.profiles if isinstance(item, dict)
                    and item.get("alias") == config.get("credential_profile")), None)
    if not profile or profile.get("revision_sha256") != config.get("credential_profile_revision_sha256"):
        return []
    ssh = config.get("ssh", {})
    if (ssh.get("host") not in profile.get("allowed_hosts", []) or ssh.get("user") not in profile.get("allowed_users", [])
            or ssh.get("port") not in profile.get("allowed_ports", [])):
        return []
    try:
        if "storage_roots" in profile:
            return validate_storage_roots(profile["storage_roots"])
        # Compatibility with existing public worker metadata. These are exact
        # choices, never a free-text descendant path or a new SSH authorization.
        return validate_storage_roots([
            {"alias": f"staging-root-{index}", "path": canonical_root(path), "roles": list(ROLES),
             "capabilities": {"visibility": "unknown", "formats": ["directory"], "quota_reporting": "unknown"}}
            for index, path in enumerate(profile.get("remote_root_prefixes", [])[:32])])
    except (StoragePlanningError, TypeError, AttributeError):
        return []


def target_current(target):
    connection = target.connection
    if (not target.enabled or not target.pool.enabled or target.owner_id != connection.owner_id
            or target.pool.owner_id != target.owner_id or target.pool.connection_id != connection.id
            or target.connection_revision_sha256 != connection.revision_sha256
            or target.profile_revision_sha256 != connection.configuration.get("credential_profile_revision_sha256")):
        return False
    root = next((item for item in protected_roots(connection) if item["alias"] == target.root_alias), None)
    return bool(root and root["path"] == target.root and root["roles"] == target.roles and root["capabilities"] == target.capabilities
                and (target.pool.expected_filesystem_id is None or root["filesystem_id"] == target.pool.expected_filesystem_id))


def register_target(user, connection, root_alias, name, *, pool=None):
    if not user.is_superuser and connection.owner_id != user.id:
        raise BrokerError("STORAGE_TARGET_DENIED", "Choose a cluster you own.", 403)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise BrokerError("INVALID_STORAGE", "Choose a storage target name.")
    with transaction.atomic():
        connection = Connection.objects.select_for_update().select_related("worker").get(id=connection.id)
        root = next((item for item in protected_roots(connection) if item["alias"] == root_alias), None)
        if root is None:
            raise BrokerError("STORAGE_ROOT_DENIED", "Choose a current root published by the protected worker.", 403)
        if pool and (pool.owner_id != connection.owner_id or pool.connection_id != connection.id or not pool.enabled
                     or pool.expected_filesystem_id != root["filesystem_id"]):
            raise BrokerError("STORAGE_POOL_DENIED", "Use a compatible pool on the same cluster.", 403)
        existing = StorageTarget.objects.filter(connection=connection, root_alias=root_alias,
            profile_revision_sha256=connection.configuration["credential_profile_revision_sha256"],
            connection_revision_sha256=connection.revision_sha256).first()
        if existing:
            if pool and existing.pool_id != pool.id:
                raise BrokerError("STORAGE_TARGET_EXISTS", "This enrolled target already belongs to another capacity pool.", 409)
            return existing
        if pool is None:
            pool = StorageCapacityPool.objects.create(owner=connection.owner, connection=connection,
                name=name.strip(), probe_root=root["path"], expected_filesystem_id=root["filesystem_id"])
        target = StorageTarget.objects.create(owner=connection.owner, connection=connection, pool=pool, name=name.strip(),
            root_alias=root_alias, root=root["path"], roles=root["roles"], capabilities=root["capabilities"],
            profile_revision_sha256=connection.configuration["credential_profile_revision_sha256"],
            connection_revision_sha256=connection.revision_sha256)
        audit(None, user.id, "storage.target_enrolled", target.id, {"connection_id": str(connection.id), "root_alias": root_alias})
        return target


def selected_targets(project, connection):
    if not project.connections.filter(id=connection.id, enabled=True).exists():
        return StorageTarget.objects.none()
    return StorageTarget.objects.select_related("pool", "connection__worker").filter(
        owner_id=project.owner_id, connection=connection, enabled=True, pool__enabled=True)


def save_project_storage(user, project, connection, data):
    human_permission(user, project, "project_admin")
    if not project.connections.filter(id=connection.id, enabled=True).exists():
        raise BrokerError("STORAGE_TARGET_DENIED", "Choose a cluster assigned to this project.", 403)
    for role in ROLES:
        target = data.get(role)
        if target and (target.owner_id != project.owner_id or target.connection_id != connection.id
                       or role not in target.roles or not target_current(target)):
            raise BrokerError("STORAGE_TARGET_DENIED", "Choose a current approved target for each role.", 403)
    with transaction.atomic():
        Project.objects.select_for_update().get(id=project.id)
        budget, _ = ProjectStorageBudget.objects.get_or_create(project=project)
        for field in ("bytes_budget", "inodes_budget"):
            setattr(budget, field, integer(data.get(field), nullable=True))
        for field in ("retained_bytes_estimate", "retained_inodes_estimate"):
            setattr(budget, field, integer(data.get(field, 0)))
        budget.estimate_updated_at, budget.estimate_source = timezone.now(), "human_estimate"
        budget.save()
        binding, _ = ProjectStorageBinding.objects.get_or_create(project=project, connection=connection)
        for role in ROLES:
            setattr(binding, role, data.get(role))
        binding.save()
        audit(project, user.id, "storage.plan_settings_updated", project.id, {"connection_id": str(connection.id), "advisory_only": True})
        return binding


def _reserved(queryset):
    values = queryset.filter(state="held").aggregate(bytes=Sum("bytes_estimate"), inodes=Sum("inodes_estimate"))
    return {unit: integer(value or 0) for unit, value in values.items()}


def inventory(project, *, credential=None, now=None):
    now = now or timezone.now()
    budget = ProjectStorageBudget.objects.filter(project=project).first()
    reserved = _reserved(StorageEstimateReservation.objects.filter(project=project))
    budget_data = {"bytes_budget": budget.bytes_budget if budget else None, "inodes_budget": budget.inodes_budget if budget else None,
        "retained_bytes_estimate": budget.retained_bytes_estimate if budget else 0,
        "retained_inodes_estimate": budget.retained_inodes_estimate if budget else 0,
        "reserved_bytes_estimate": reserved["bytes"], "reserved_inodes_estimate": reserved["inodes"],
        "estimate_updated_at": budget.estimate_updated_at.isoformat() if budget and budget.estimate_updated_at else None,
        "estimate_source": budget.estimate_source if budget else "not_estimated"}
    bindings, targets = [], {}
    rows = ProjectStorageBinding.objects.filter(project=project, connection__enabled=True).select_related("connection")
    for row in rows:
        if not project.connections.filter(id=row.connection_id, enabled=True).exists() or credential and not connection_allowed(credential, row.connection_id):
            continue
        choices = {}
        for role in ROLES:
            target = getattr(row, role)
            if target and target.owner_id == project.owner_id and target.connection_id == row.connection_id and role in target.roles and target_current(target):
                choices[role] = str(target.id)
                targets[str(target.id)] = target
            else:
                choices[role] = None
        bindings.append({"connection_id": str(row.connection_id), "targets": choices})
    pools = {}
    for target in targets.values():
        pool = target.pool
        if str(pool.id) in pools:
            continue
        held = _reserved(StorageEstimateReservation.objects.filter(target__pool=pool))
        health = assess_health(pool.observation, pool.health_policy, now=now, previous_state=pool.health_state,
                               recovery_since=pool.recovery_since.isoformat() if pool.recovery_since else None)
        pools[str(pool.id)] = {"id": str(pool.id), "name": pool.name, "observation": pool.observation,
            "health_policy": pool.health_policy, "health_state": pool.health_state,
            "recovery_since": pool.recovery_since.isoformat() if pool.recovery_since else None, "health": health,
            "reserved_bytes_estimate": held["bytes"], "reserved_inodes_estimate": held["inodes"],
            "measurement_status": "cached" if pool.observation else "not_observed"}
    return {"schema_version": "1.0", "project_id": str(project.id), "advisory_only": True,
        "enforced_at_dispatch": False, "probe_wired": False, "budget": budget_data, "bindings": bindings,
        "targets": [{"id": str(target.id), "name": target.name, "connection_id": str(target.connection_id),
                     "pool_id": str(target.pool_id), "roles": target.roles, "capabilities": target.capabilities}
                    for target in targets.values()], "pools": list(pools.values()),
        "note": "Estimates and cached observations do not enforce OS quotas or change where existing jobs write."}


def plan_for_credential(credential, document):
    from compute_broker.control.auth import require_connection
    try:
        connection_id = str(UUID(document.get("connection_id", "")))
    except (ValueError, TypeError, AttributeError):
        raise BrokerError("INVALID_STORAGE_PLAN", "Select a cluster identifier.") from None
    require_connection(credential, connection_id)
    try:
        return plan_storage(document, inventory(credential.project, credential=credential))
    except StoragePlanningError as error:
        raise BrokerError(error.code, error.message, 403 if error.code == "STORAGE_TARGET_DENIED" else 400) from None


def record_observation(worker, pool_id, document):
    """Integration hook only: caller must authenticate a fixed protected probe.

    Not exposed through an HTTP route in this slice. No caller-supplied path is
    used. Unpinned filesystem identities remain explicitly unverified.
    """
    try:
        data = validate_observation(document)
    except StoragePlanningError as error:
        raise BrokerError(error.code, error.message) from None
    with transaction.atomic():
        pool = StorageCapacityPool.objects.select_for_update().select_related("connection__worker").filter(
            id=pool_id, connection__worker=worker, enabled=True).first()
        if pool is None:
            raise BrokerError("NOT_FOUND", "Storage pool not found.", 404)
        current = any(target.root == pool.probe_root and target_current(target) for target in pool.storagetarget_set.select_related("pool", "connection__worker"))
        if not current or worker.revoked:
            raise BrokerError("STORAGE_TARGET_DENIED", "The protected target binding is no longer current.", 403)
        if pool.observation and timestamp(data["observed_at"]) <= timestamp(pool.observation["observed_at"]):
            raise BrokerError("STALE_OBSERVATION", "Capacity observations must move forward.", 409)
        if pool.expected_filesystem_id is None:
            data["identity_verified"] = False
        elif data["filesystem_id"] != pool.expected_filesystem_id:
            data.update(identity_verified=False, status="unavailable", error_code="FILESYSTEM_IDENTITY_MISMATCH")
        health = assess_health(data, pool.health_policy, previous_state=pool.health_state,
                               recovery_since=pool.recovery_since.isoformat() if pool.recovery_since else None,
                               previous_observed_at=pool.observation.get("observed_at"))
        previous_state = pool.health_state
        pool.observation, pool.health_state = data, health["state"]
        if health["state"] in {"unknown", "stale", "unavailable"} and previous_state in {"critical", "emergency", "recovering"}:
            pool.health_state = previous_state  # Keep pressure memory across probe failures.
        pool.recovery_since = timestamp(health["recovery_since"]) if health["recovery_since"] else None
        pool.save(update_fields=["observation", "health_state", "recovery_since"])
        return {"accepted": True, "health": health}


def reserve_estimate(project, target, reference, peak_bytes, peak_inodes):
    """Optional integration ledger: estimated growth, never an OS reservation.

    Currently unused by job dispatch/builds. Integrators must prove that their
    actual write path is derived from this target before calling it.
    """
    reference, peak_bytes, peak_inodes = UUID(str(reference)), integer(peak_bytes), integer(peak_inodes)
    with transaction.atomic():
        Project.objects.select_for_update().get(id=project.id)
        target = StorageTarget.objects.select_related("connection__worker", "pool").get(id=target.id)
        existing = StorageEstimateReservation.objects.filter(reference=reference).first()
        if existing:
            if (existing.project_id, existing.target_id, existing.bytes_estimate, existing.inodes_estimate) != (project.id, target.id, peak_bytes, peak_inodes):
                raise BrokerError("STORAGE_RESERVATION_CONFLICT", "The reference already identifies a different estimate.", 409)
            return existing
        if (target.owner_id != project.owner_id or not target_current(target)
                or not project.connections.filter(id=target.connection_id, enabled=True).exists()
                or not any(getattr(binding, f"{role}_id") == target.id for binding in ProjectStorageBinding.objects.filter(
                    project=project, connection_id=target.connection_id) for role in ROLES)):
            raise BrokerError("STORAGE_TARGET_DENIED", "The target is not selected for this project.", 403)
        StorageCapacityPool.objects.select_for_update().get(id=target.pool_id)
        budget = ProjectStorageBudget.objects.filter(project=project).first()
        reserved = _reserved(StorageEstimateReservation.objects.filter(project=project))
        if budget:
            for unit, requested in (("bytes", peak_bytes), ("inodes", peak_inodes)):
                projected = total([getattr(budget, f"retained_{unit}_estimate"), reserved[unit], requested])
                limit = getattr(budget, f"{unit}_budget")
                if limit is not None and projected > limit:
                    raise BrokerError("STORAGE_BUDGET_EXCEEDED", "The declared growth exceeds the advisory project budget.", 409)
        return StorageEstimateReservation.objects.create(project=project, target=target, reference=reference,
                                                          bytes_estimate=peak_bytes, inodes_estimate=peak_inodes)


def release_estimate(project, reference):
    with transaction.atomic():
        Project.objects.select_for_update().get(id=project.id)
        reservation = StorageEstimateReservation.objects.select_for_update().filter(project=project, reference=reference).first()
        if reservation and reservation.state == "held":
            reservation.state, reservation.released_at = "released", timezone.now()
            reservation.save(update_fields=["state", "released_at"])
        return reservation
