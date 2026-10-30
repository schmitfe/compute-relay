"""Project-scoped advisory metadata; no SSH, file transfer, deletion, or dispatch."""

from __future__ import annotations

from uuid import UUID
from datetime import timedelta
import posixpath

from django.db import transaction
from django.utils import timezone

from compute_relay.control.auth import connection_allowed, human_permission
from compute_relay.control.errors import BrokerError
from compute_relay.control.models import Connection, Project, User, WorkerIdentity
from compute_relay.control.services import audit, digest
from .models import (ProjectStorageBinding, ProjectStorageBudget, StorageCapacityPool,
                     StorageObservationLease, StorageTarget, StorageTargetRegistration)
from .planning import (ROLES, StoragePlanningError, assess_health, canonical_root, integer,
                       plan_storage, roots_from_metadata, timestamp, validate_observation)


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
        # Published roots are exact choices, never a free-text descendant path
        # or a new SSH authorization.
        return roots_from_metadata(profile)
    except (StoragePlanningError, TypeError, AttributeError):
        return []


def confirmed_directory_root(registration):
    """Validate a canonical receipt, allowing the worker to resolve a parent alias."""
    task = registration.task
    result = task.result
    if not isinstance(result, dict) or not isinstance(task.request, dict):
        return None
    try:
        root = canonical_root(result.get("path"))
    except StoragePlanningError:
        return None
    if (len(root) > 240 or result.get("ok") is not True or result.get("writable") is not True
            or type(result.get("created")) is not bool or result.get("parent") != posixpath.dirname(root)
            or task.request != {"path": posixpath.dirname(registration.root), "name": posixpath.basename(registration.root), "parents": True}
            or posixpath.basename(root) != task.request["name"]):
        return None
    return root


def _project_connection(user, project, connection):
    if project is None or not getattr(project, "id", None):
        raise BrokerError("STORAGE_PROJECT_REQUIRED", "Select a project for this storage location.")
    human_permission(user, project, "project_admin")
    if (not connection.enabled or connection.archived
            or not project.connections.filter(id=connection.id, enabled=True, archived=False).exists()):
        raise BrokerError("STORAGE_TARGET_DENIED", "Choose an enabled cluster assigned to this project.", 403)


def _fill_empty_defaults(project, target):
    """The caller holds the project lock; preserve existing choices and budgets."""
    binding, _ = ProjectStorageBinding.objects.get_or_create(project=project, connection=target.connection)
    fields = []
    for role in ROLES:
        if role in target.roles and getattr(binding, f"{role}_id") is None:
            setattr(binding, role, target)
            fields.append(role)
    if fields:
        binding.save(update_fields=fields)


def target_current(target):
    connection = target.connection
    if (not connection.enabled or connection.archived or connection.worker.revoked
            or connection.worker.owner_id != connection.owner_id
            or not target.enabled or not target.pool.enabled
            or target.owner_id != connection.owner_id
            or target.pool.owner_id != target.owner_id or target.pool.connection_id != connection.id
            or target.connection_revision_sha256 != connection.revision_sha256
            or target.profile_revision_sha256 != connection.configuration.get("credential_profile_revision_sha256")):
        return False
    if not Project.objects.filter(id=target.project_id, connections=connection).exists():
        return False
    registration = StorageTargetRegistration.objects.filter(target=target).select_related("task").first()
    if registration:
        from compute_relay.control.connector_setup import personal
        profile = next((item for item in connection.worker.profiles if isinstance(item, dict)
            and item.get("alias") == connection.configuration.get("credential_profile")), {})
        return bool(personal(connection) and registration.owner_id == target.owner_id
            and registration.project_id == target.project_id
            and registration.connection_id == connection.id and confirmed_directory_root(registration) == target.root
            and registration.roles == target.roles and registration.task.state == "succeeded"
            and registration.task.kind == "mkdir" and registration.task.owner_id == target.owner_id
            and registration.task.connection_id == connection.id
            and registration.task.connection_revision == target.connection_revision_sha256
            and registration.profile_revision_sha256 == profile.get("revision_sha256") == target.profile_revision_sha256
            and registration.connection_revision_sha256 == target.connection_revision_sha256)
    root = next((item for item in protected_roots(connection) if item["alias"] == target.root_alias), None)
    return bool(root and root["path"] == target.root and target.roles == root["roles"]
                and root["capabilities"] == target.capabilities
                and (target.pool.expected_filesystem_id is None or root["filesystem_id"] == target.pool.expected_filesystem_id))


def queue_personal_target(user, connection, root, name, roles, revision, submission_id, *, project):
    """Create the human-selected directory; this does not expand worker job scope."""
    from compute_relay.control.connector_setup import enqueue, locked_connection, own_connection
    _project_connection(user, project, connection)
    reference = own_connection(user, connection.id)
    try:
        root = canonical_root(root)
        identifier = UUID(str(submission_id))
    except (StoragePlanningError, ValueError, TypeError):
        raise BrokerError("INVALID_STORAGE", "Reload the form and choose a dedicated canonical directory.") from None
    if (len(root) > 240 or not isinstance(name, str) or not 1 <= len(name.strip()) <= 100
            or not isinstance(roles, list) or not roles or any(role not in ROLES for role in roles)
            or len(set(roles)) != len(roles) or len(posixpath.basename(root)) > 80):
        raise BrokerError("INVALID_STORAGE", "Choose a name, planning uses and a directory with a final component up to 80 characters.")
    payload = {"path": posixpath.dirname(root), "name": posixpath.basename(root), "parents": True}
    with transaction.atomic():
        current, _, _ = locked_connection(reference)
        # locked_connection already locks assigned projects before Connection.
        # Do not acquire a new project lock after it if the assignment changed.
        project = Project.objects.get(id=project.id)
        _project_connection(user, project, current)
        existing = StorageTargetRegistration.objects.select_for_update().filter(id=identifier).first()
        if existing:
            if (existing.owner_id != user.id or existing.connection_id != current.id
                    or existing.project_id != project.id):
                raise BrokerError("NOT_FOUND", "Storage registration not found.", 404)
            if (existing.root, existing.name, existing.roles, existing.connection_revision_sha256) != (root, name.strip(), roles, revision):
                raise BrokerError("STORAGE_REGISTRATION_CONFLICT", "This form already identifies a different directory request. Reload it.", 409)
            return existing
        from compute_relay.control.remote_cleanup import protect_registration
        protect_registration(current, [root])
        task = enqueue(user, current.id, "mkdir", payload, revision)
        return StorageTargetRegistration.objects.create(id=identifier, owner=user, project=project, connection=current,
            task=task, name=name.strip(), root=root, roles=roles,
            profile_revision_sha256=current.configuration["credential_profile_revision_sha256"],
            connection_revision_sha256=current.revision_sha256)


def finish_personal_target(user, registration, *, confirmed_root=None):
    """An explicit human POST accepts only its own exact fenced worker receipt."""
    from compute_relay.control.connector_setup import locked_connection, own_connection
    from compute_relay.control.models import ConnectorTask
    reference = own_connection(user, registration.connection_id)
    project = Project.objects.get(id=registration.project_id)
    _project_connection(user, project, reference)
    with transaction.atomic():
        connection, _, _ = locked_connection(reference)
        registration = StorageTargetRegistration.objects.select_for_update().get(id=registration.id, owner=user, connection=connection)
        if registration.project_id != project.id:
            raise BrokerError("STORAGE_TARGET_DENIED", "The registration's project changed. Reload its configuration.", 409)
        project = Project.objects.get(id=project.id)
        _project_connection(user, project, connection)
        if registration.target_id:
            target = registration.target
            if target.project_id != project.id or target.owner_id != connection.owner_id or not target_current(target):
                raise BrokerError("STORAGE_TARGET_DENIED", "This storage location is no longer current for this project.", 403)
            return target
        task = ConnectorTask.objects.select_for_update().get(id=registration.task_id)
        registration.task = task
        result = task.result
        resolved_root = confirmed_directory_root(registration)
        if (registration.connection_revision_sha256 != connection.revision_sha256
                or registration.profile_revision_sha256 != connection.configuration.get("credential_profile_revision_sha256")
                or task.connection_revision != connection.revision_sha256 or task.connection_id != connection.id
                or task.owner_id != user.id or task.kind != "mkdir" or task.state != "succeeded"
                or task.expires_at <= timezone.now()
                or resolved_root is None or confirmed_root != resolved_root):
            raise BrokerError("STORAGE_DIRECTORY_UNCONFIRMED", "Review and confirm the worker's exact resolved writable directory under the current cluster configuration before saving.", 409)
        profile = next((item for item in connection.worker.profiles if isinstance(item, dict)
            and item.get("alias") == connection.configuration.get("credential_profile")), {})
        if profile.get("revision_sha256") != registration.profile_revision_sha256:
            raise BrokerError("STORAGE_DIRECTORY_UNCONFIRMED", "The connector profile changed. Start a new directory registration.", 409)
        from compute_relay.control.remote_cleanup import protect_registration
        protect_registration(connection, [resolved_root])
        pool = StorageCapacityPool.objects.create(owner=user, connection=connection, name=registration.name,
            probe_root=resolved_root, expected_filesystem_id=None, probe_enabled=False)
        target = StorageTarget.objects.create(owner=connection.owner, project=project, connection=connection, pool=pool, name=registration.name,
            root_alias="personal-" + registration.id.hex, root=resolved_root, roles=registration.roles,
            capabilities={"visibility": "unknown", "formats": ["directory"], "quota_reporting": "unknown"},
            profile_revision_sha256=registration.profile_revision_sha256,
            connection_revision_sha256=registration.connection_revision_sha256)
        registration.target = target
        registration.save(update_fields=["target"])
        _fill_empty_defaults(project, target)
        audit(project, user.id, "storage.personal_target_enrolled", target.id,
              {"connection_id": str(connection.id), "task_id": str(task.id), "created": result["created"]})
        return target


def probe_supported(target):
    return ("storage" in target.connection.worker.supported_extensions
            and not StorageTargetRegistration.objects.filter(target=target).exists())


def register_target(user, connection, root_alias, name, *, project, pool=None, enable_probes=False):
    _project_connection(user, project, connection)
    if connection.owner_id != user.id:
        raise BrokerError("STORAGE_TARGET_DENIED", "Choose a cluster you own.", 403)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise BrokerError("INVALID_STORAGE", "Choose a storage target name.")
    with transaction.atomic():
        User.objects.select_for_update(no_key=True).get(id=user.id)
        project = Project.objects.select_for_update().get(id=project.id)
        connection = Connection.objects.select_for_update(of=("self",)).select_related("worker").get(id=connection.id)
        _project_connection(user, project, connection)
        root = next((item for item in protected_roots(connection) if item["alias"] == root_alias), None)
        if root is None:
            raise BrokerError("STORAGE_ROOT_DENIED", "Choose a current root published by the protected worker.", 403)
        if pool and (pool.owner_id != connection.owner_id or pool.connection_id != connection.id or not pool.enabled
                     or pool.expected_filesystem_id != root["filesystem_id"]):
            raise BrokerError("STORAGE_POOL_DENIED", "Use a compatible pool on the same cluster.", 403)
        existing = StorageTarget.objects.filter(project=project, connection=connection, root_alias=root_alias,
            profile_revision_sha256=connection.configuration["credential_profile_revision_sha256"],
            connection_revision_sha256=connection.revision_sha256).first()
        if existing:
            if pool and existing.pool_id != pool.id:
                raise BrokerError("STORAGE_TARGET_EXISTS", "This enrolled target already belongs to another capacity pool.", 409)
            if not target_current(existing):
                raise BrokerError("STORAGE_TARGET_STALE", "The saved location no longer matches the current project and protected root. Refresh its cluster configuration before enrolling it.", 409)
            if enable_probes and not existing.pool.probe_enabled:
                set_probe_enabled(user, existing.pool, True)
            return existing
        from compute_relay.control.remote_cleanup import protect_registration
        protect_registration(connection, [root["path"]])
        if pool is None:
            pool = StorageCapacityPool.objects.create(owner=connection.owner, connection=connection,
                name=name.strip(), probe_root=root["path"], expected_filesystem_id=root["filesystem_id"])
        target = StorageTarget.objects.create(owner=connection.owner, project=project, connection=connection, pool=pool, name=name.strip(),
            root_alias=root_alias, root=root["path"], roles=root["roles"], capabilities=root["capabilities"],
            profile_revision_sha256=connection.configuration["credential_profile_revision_sha256"],
            connection_revision_sha256=connection.revision_sha256)
        _fill_empty_defaults(project, target)
        audit(project, user.id, "storage.target_enrolled", target.id, {"connection_id": str(connection.id), "root_alias": root_alias})
        if enable_probes:
            set_probe_enabled(user, pool, True)
        return target


def selected_targets(project, connection):
    if not project.connections.filter(id=connection.id, enabled=True, archived=False).exists():
        return StorageTarget.objects.none()
    return StorageTarget.objects.select_related("pool", "connection__worker").filter(
        project=project, owner_id=connection.owner_id, connection=connection, enabled=True, pool__enabled=True)


def save_project_storage(user, project, connection, data):
    _project_connection(user, project, connection)
    with transaction.atomic():
        project = Project.objects.select_for_update().get(id=project.id)
        connection = Connection.objects.select_for_update(of=("self",)).select_related("worker").get(id=connection.id)
        _project_connection(user, project, connection)
        choices = {}
        for role in ROLES:
            selected = data.get(role)
            target = (StorageTarget.objects.select_for_update(of=("self",)).select_related("pool", "connection__worker").filter(
                id=getattr(selected, "id", None), project=project, connection=connection,
                owner_id=connection.owner_id).first()) if selected else None
            if selected and (target is None or target.project_id != project.id
                    or target.owner_id != connection.owner_id or target.connection_id != connection.id
                    or role not in target.roles or not target_current(target)):
                raise BrokerError("STORAGE_TARGET_DENIED", "Choose a current approved target for each role in this project.", 403)
            choices[role] = target
        budget, _ = ProjectStorageBudget.objects.get_or_create(project=project)
        for field in ("bytes_budget", "inodes_budget"):
            setattr(budget, field, integer(data.get(field), nullable=True))
        for field in ("retained_bytes_estimate", "retained_inodes_estimate"):
            setattr(budget, field, integer(data.get(field, 0)))
        budget.estimate_updated_at, budget.estimate_source = timezone.now(), "human_estimate"
        budget.save()
        binding, _ = ProjectStorageBinding.objects.get_or_create(project=project, connection=connection)
        for role in ROLES:
            setattr(binding, role, choices[role])
        binding.save()
        audit(project, user.id, "storage.plan_settings_updated", project.id, {"connection_id": str(connection.id), "advisory_only": True})
        return binding


def inventory(project, *, credential=None, now=None):
    now = now or timezone.now()
    budget = ProjectStorageBudget.objects.filter(project=project).first()
    budget_data = {"bytes_budget": budget.bytes_budget if budget else None, "inodes_budget": budget.inodes_budget if budget else None,
        "retained_bytes_estimate": budget.retained_bytes_estimate if budget else 0,
        "retained_inodes_estimate": budget.retained_inodes_estimate if budget else 0,
        "estimate_updated_at": budget.estimate_updated_at.isoformat() if budget and budget.estimate_updated_at else None,
        "estimate_source": budget.estimate_source if budget else "not_estimated"}
    bindings, targets = [], {}
    rows = ProjectStorageBinding.objects.filter(project=project, connection__enabled=True, connection__archived=False).select_related("connection")
    for row in rows:
        if not project.connections.filter(id=row.connection_id, enabled=True).exists() or credential and not connection_allowed(credential, row.connection_id):
            continue
        choices = {}
        for role in ROLES:
            target = getattr(row, role)
            if (target and target.project_id == project.id and target.owner_id == row.connection.owner_id
                    and target.connection_id == row.connection_id and role in target.roles and target_current(target)):
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
        health = assess_health(pool.observation, pool.health_policy, now=now, previous_state=pool.health_state,
                               recovery_since=pool.recovery_since.isoformat() if pool.recovery_since else None)
        pools[str(pool.id)] = {"id": str(pool.id), "name": pool.name, "observation": pool.observation,
            "probe_enabled": pool.probe_enabled,
            "probe_supported": probe_supported(target),
            "next_probe_at": pool.next_probe_at.isoformat() if pool.probe_enabled else None,
            "health_policy": pool.health_policy, "health_state": pool.health_state,
            "recovery_since": pool.recovery_since.isoformat() if pool.recovery_since else None, "health": health,
            "measurement_status": "cached" if pool.observation else "not_observed"}
    return {"schema_version": "1.0", "project_id": str(project.id), "advisory_only": True,
        "enforced_at_dispatch": False, "probe_wired": True, "budget": budget_data, "bindings": bindings,
        "targets": [{"id": str(target.id), "project_id": str(target.project_id), "name": target.name, "connection_id": str(target.connection_id),
                     "pool_id": str(target.pool_id), "roles": target.roles, "capabilities": target.capabilities}
                    for target in targets.values()], "pools": list(pools.values()),
        "note": "Estimates and cached observations do not enforce OS quotas or change where existing jobs write."}


def plan_for_credential(credential, document):
    from compute_relay.control.auth import require_connection
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

    The fenced worker report calls this hook; no agent-supplied path is used.
    Unpinned filesystem identities remain explicitly unverified.
    """
    try:
        data = validate_observation(document)
    except StoragePlanningError as error:
        raise BrokerError(error.code, error.message) from None
    with transaction.atomic():
        pool = StorageCapacityPool.objects.select_for_update(of=("self",)).select_related("connection__worker").filter(
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


def set_probe_enabled(user, pool, enabled):
    if type(enabled) is not bool:
        raise BrokerError("INVALID_STORAGE", "Choose whether read-only capacity checks are enabled.")
    if pool.owner_id != user.id:
        raise BrokerError("STORAGE_TARGET_DENIED", "Only the location owner can enable capacity checks.", 403)
    if enabled and StorageTargetRegistration.objects.filter(target__pool=pool).exists():
        raise BrokerError("STORAGE_PROBE_UNSUPPORTED", "This personal directory is available for advisory planning. Capacity probes require a separately published protected root and supported storage worker.", 409)
    with transaction.atomic():
        pool = StorageCapacityPool.objects.select_for_update().get(id=pool.id)
        pool.probe_enabled, pool.next_probe_at = enabled, timezone.now()
        pool.save(update_fields=["probe_enabled", "next_probe_at"])
        if not enabled:
            StorageObservationLease.objects.filter(pool=pool, state="leased").update(state="expired")
        audit(None, user.id, "storage.probe_enabled" if enabled else "storage.probe_paused", pool.id)
        return pool


def lease_work(worker):
    """One fair, coalesced metadata probe; no project job state is involved."""
    now = timezone.now()
    with transaction.atomic():
        worker = WorkerIdentity.objects.select_for_update().get(id=worker.id)
        if worker.revoked or "storage" not in worker.supported_extensions:
            return None
        leases = StorageObservationLease.objects.filter(pool__connection__worker=worker)
        if leases.filter(state="leased", expires_at__gt=now).exists():
            return None
        # A completed maintenance call yields the worker lane to core jobs.
        if leases.filter(reported_at__gt=now - timedelta(seconds=15)).exists():
            return None
        pools = StorageCapacityPool.objects.select_for_update(of=("self",)).select_related("connection__worker").filter(
            connection__worker=worker, connection__enabled=True, enabled=True, probe_enabled=True,
            next_probe_at__lte=now).order_by("next_probe_at", "id")
        for pool in pools[:30]:
            target = next((item for item in pool.storagetarget_set.filter(personal_registration__isnull=True).select_related("pool", "connection__worker").order_by("id")
                           if item.root == pool.probe_root and target_current(item)), None)
            if target is None:
                pool.next_probe_at = now + timedelta(seconds=300)
                pool.save(update_fields=["next_probe_at"])
                continue
            StorageObservationLease.objects.filter(pool=pool, state="leased").update(state="expired")
            pool.probe_generation += 1
            pool.next_probe_at = now + timedelta(seconds=60)
            pool.save(update_fields=["probe_generation", "next_probe_at"])
            lease = StorageObservationLease.objects.create(pool=pool, target=target, generation=pool.probe_generation,
                expires_at=now + timedelta(seconds=90), profile_revision_sha256=target.profile_revision_sha256,
                connection_revision_sha256=target.connection_revision_sha256)
            context = {"pool_id": str(pool.id), "target_id": str(target.id), "connection_id": str(target.connection_id),
                       "root_alias": target.root_alias, "profile_revision_sha256": lease.profile_revision_sha256,
                       "connection_revision_sha256": lease.connection_revision_sha256}
            return {"id": str(lease.id), "generation": lease.generation,
                    "configuration": pool.connection.configuration, "context": context}
    return None


def report_probe(worker, pool_id, document):
    expected = {"lease_id", "generation", "target_id", "profile_revision_sha256", "connection_revision_sha256", "observation"}
    if not isinstance(document, dict) or set(document) != expected or type(document.get("generation")) is not int:
        raise BrokerError("INVALID_STORAGE_REPORT", "Unexpected capacity report fields.")
    payload_sha256 = digest(document)
    now = timezone.now()
    with transaction.atomic():
        worker = WorkerIdentity.objects.select_for_update().get(id=worker.id)
        if worker.revoked:
            raise BrokerError("AUTH_REQUIRED", "Worker access is revoked.", 403)
        pool = StorageCapacityPool.objects.select_for_update().filter(id=pool_id, connection__worker=worker).first()
        if pool is None:
            raise BrokerError("NOT_FOUND", "Storage pool not found.", 404)
        lease = StorageObservationLease.objects.select_for_update(of=("self",)).select_related("pool__connection__worker", "target__connection__worker", "target__pool").filter(
            id=document["lease_id"], pool_id=pool_id, pool__connection__worker=worker).first()
        if lease is None:
            raise BrokerError("NOT_FOUND", "Capacity lease not found.", 404)
        if lease.state == "reported":
            if lease.report_sha256 != payload_sha256:
                raise BrokerError("STORAGE_REPORT_CONFLICT", "This capacity lease already has a different result.", 409)
            return lease.response
        if (lease.state != "leased" or lease.expires_at <= now or not pool.probe_enabled or not pool.enabled
                or worker.revoked or "storage" not in pool.connection.worker.supported_extensions
                or lease.generation != pool.probe_generation or document["generation"] != lease.generation):
            raise BrokerError("STALE_LEASE", "Capacity lease is paused, stale or expired.", 409)
        if (str(lease.target_id) != document["target_id"] or lease.profile_revision_sha256 != document["profile_revision_sha256"]
                or lease.connection_revision_sha256 != document["connection_revision_sha256"] or not target_current(lease.target)
                or lease.target.root != pool.probe_root):
            raise BrokerError("REVISION_CHANGED", "The protected capacity target changed.", 409)
        data = validate_observation(document["observation"], now=now)
        # A pre-existing or replayed sample cannot satisfy a new probe lease.
        if timestamp(data["observed_at"]) < lease.created_at - timedelta(seconds=30):
            raise BrokerError("STALE_OBSERVATION", "Capacity sample predates its lease.", 409)
        response = record_observation(worker, pool.id, data)
        lease.state, lease.reported_at, lease.report_sha256, lease.response = "reported", now, payload_sha256, response
        lease.save(update_fields=["state", "reported_at", "report_sha256", "response"])
        pool.refresh_from_db()
        pool.next_probe_at = now + timedelta(seconds=60 if pool.observation["status"] == "available" else 300)
        pool.save(update_fields=["next_probe_at"])
        return response
