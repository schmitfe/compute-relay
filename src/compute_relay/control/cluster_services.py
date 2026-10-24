"""Leased, read-only discovery through the credential-holding worker."""

from copy import deepcopy
import re
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .errors import BrokerError
from .models import Attempt, Connection, ConnectorTask, Project, WorkerIdentity
from .services import audit, digest

PUBLIC_CONFIG_FIELDS = {"backend", "credential_profile", "ssh", "remote_root", "profile_revision", "site_profile", "credential_profile_revision_sha256"}
PROFILE_FIELDS = {"alias", "backend", "revision_sha256", "allowed_hosts", "allowed_users", "allowed_ports", "remote_root_prefixes", "site_profile"}
SITE_FIELDS = {"partitions", "accounts", "qos", "allocated_cpus", "allocated_gpus", "cpu_allocation_quantum", "maximum_cpus", "maximum_gpus", "maximum_memory_mib", "maximum_time_limit_seconds", "automatic_execution_allowed"}


def publish_profiles(worker, document):
    from compute_relay.extensions import EXTENSIONS
    extensions = document.get("extensions", [])
    if not isinstance(extensions, list) or any(not isinstance(name, str) or name not in EXTENSIONS for name in extensions) or len(set(extensions)) != len(extensions):
        raise BrokerError("INVALID_PROFILES", "Invalid worker extension capabilities.")
    profiles = deepcopy(document.get("profiles"))
    if not isinstance(profiles, list) or len(profiles) > 100:
        raise BrokerError("INVALID_PROFILES", "Invalid worker profiles.")
    aliases = set()
    for profile in profiles:
        if not isinstance(profile, dict) or set(profile) != PROFILE_FIELDS:
            raise BrokerError("INVALID_PROFILES", "Invalid worker profile fields.")
        alias = profile["alias"]
        if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}", alias) or alias in aliases:
            raise BrokerError("INVALID_PROFILES", "Invalid profile alias.")
        aliases.add(alias)
        if profile["backend"] not in {"slurm", "fake"} or not re.fullmatch(r"[0-9a-f]{64}", profile["revision_sha256"]):
            raise BrokerError("INVALID_PROFILES", "Invalid worker profile binding.")
        if not isinstance(profile["site_profile"], dict) or set(profile["site_profile"]) - SITE_FIELDS:
            raise BrokerError("INVALID_PROFILES", "Invalid site profile.")
        if "automatic_execution_allowed" in profile["site_profile"] and type(profile["site_profile"]["automatic_execution_allowed"]) is not bool:
            raise BrokerError("INVALID_PROFILES", "Invalid automatic execution setting.")
        unit = profile["site_profile"].get("cpu_allocation_quantum", 1)
        if type(unit) is not int or not 1 <= unit <= 1024:
            raise BrokerError("INVALID_PROFILES", "Invalid CPU allocation unit.")
        for field in ("allowed_hosts", "allowed_users", "allowed_ports", "remote_root_prefixes"):
            if not isinstance(profile[field], list) or not 1 <= len(profile[field]) <= 100:
                raise BrokerError("INVALID_PROFILES", "Invalid worker target scope.")
    WorkerIdentity.objects.filter(id=worker.id).update(profiles=profiles, supported_extensions=extensions, last_seen_at=timezone.now())
    return {"accepted": True}


def lease_discovery(worker):
    now = timezone.now()
    with transaction.atomic():
        connection = Connection.objects.select_for_update().filter(worker=worker, enabled=True, archived=False, discovery_state__in=["queued", "discovering"]).filter(
            Q(discovery_lease_expires_at__isnull=True) | Q(discovery_lease_expires_at__lte=now)).order_by("id").first()
        if connection is None:
            return None
        connection.discovery_generation += 1
        connection.discovery_lease_id = uuid.uuid4()
        connection.discovery_lease_expires_at = now + timedelta(seconds=180)
        connection.discovery_state = "discovering"
        connection.save(update_fields=["discovery_generation", "discovery_lease_id", "discovery_lease_expires_at", "discovery_state"])
        return {"lease": {"id": str(connection.discovery_lease_id), "operation": "discover", "connection_id": str(connection.id),
                           "generation": connection.discovery_generation, "configuration": connection.configuration}}


def report_discovery(worker, connection_id, document):
    import rfc8785
    with transaction.atomic():
        # Authenticated identity objects may predate a revocation/profile update.
        # Pin current worker authority while accepting a discovery binding.
        worker = WorkerIdentity.objects.select_for_update().filter(id=worker.id, revoked=False).first()
        if worker is None:
            raise BrokerError("AUTH_REQUIRED", "Worker access is revoked.", 403)
        list(Project.objects.select_for_update(of=("self",)).filter(connections=connection_id).order_by("id"))
        connection = Connection.objects.select_for_update().filter(id=connection_id, worker=worker, archived=False).first()
        if connection is None:
            raise BrokerError("NOT_FOUND", "Connection not found.", 404)
        if (str(connection.discovery_lease_id) != document.get("lease_id") or connection.discovery_generation != document.get("generation")
                or not connection.discovery_lease_expires_at or connection.discovery_lease_expires_at <= timezone.now()):
            raise BrokerError("STALE_LEASE", "Discovery lease is stale.", 409)
        if document.get("outcome") == "discovered":
            config = document.get("configuration")
            if not isinstance(config, dict) or set(config) != PUBLIC_CONFIG_FIELDS or set(config.get("ssh", {})) != {"host", "user", "port"}:
                raise BrokerError("INVALID_DISCOVERY", "Discovery configuration has unexpected fields.")
            if any(config.get(key) != connection.configuration.get(key) for key in ("backend", "credential_profile", "ssh", "remote_root")):
                raise BrokerError("REVISION_CHANGED", "Discovery target changed.", 409)
            profile = next((p for p in worker.profiles if p.get("alias") == config["credential_profile"]), None)
            reported_site = config.get("site_profile")
            if (not isinstance(reported_site, dict) or set(reported_site) - SITE_FIELDS or not profile
                    or config["credential_profile_revision_sha256"] != profile["revision_sha256"]
                    or reported_site != profile["site_profile"]):
                raise BrokerError("REVISION_CHANGED", "Protected profile changed during discovery.", 409)
            revision = digest(config)
            if revision != document.get("connection_revision_sha256"):
                raise BrokerError("INVALID_DISCOVERY", "Discovery binding does not match.")
            if revision != connection.revision_sha256 and Attempt.objects.filter(connection=connection).exclude(
                    accounting_state__in=["settled", "released"]).exists():
                raise BrokerError("CONNECTION_BUSY", "Finish accounting for existing attempts before changing this cluster configuration.", 409)
            snapshot = document.get("discovery_snapshot")
            if not isinstance(snapshot, dict) or len(rfc8785.dumps(snapshot)) > 1024 * 1024:
                raise BrokerError("DISCOVERY_LIMIT", "Discovery exceeds the snapshot limit.", 413)
            connection.configuration = config
            connection.revision_sha256 = revision
            connection.setup_complete = connection.configuration_confirmed and not ConnectorTask.objects.filter(
                connection=connection, kind="configure", state__in=["queued", "running"]).exists()
            connection.discovery_snapshot = snapshot
            connection.discovery_state, connection.discovery_error, connection.auth_state = "ready", "", "ready"
        elif document.get("outcome") == "discovery_failed":
            error = document.get("error_code", "DISCOVERY_UNAVAILABLE")
            if not isinstance(error, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", error):
                error = "DISCOVERY_UNAVAILABLE"
            connection.discovery_state, connection.discovery_error, connection.auth_state = "failed", error, "attention_required"
        else:
            raise BrokerError("INVALID_DISCOVERY", "Unknown discovery result.")
        connection.discovery_lease_expires_at = None
        connection.save()
        audit(None, worker.id, "connection.discovery_reported", connection.id, {"state": connection.discovery_state})
        return {"accepted": True, "state": connection.discovery_state}
