"""Explicit operator verification for quarantined legacy key owners, offline only.

This command does not infer owners, retrieve tokens, change key scopes, create
memberships, restart a service, or override unresolved remote executions.
"""

import argparse
import json
import os
import sys
from uuid import UUID

from migrate_control_db import (MigrationRefused, check_schema, digest,
    migrations, model_inventory, output_path, private_read,
    quiescent_and_balanced, require, write_committed_report)


def load_mapping(path):
    from compute_broker.domain.validation import strict_json
    document = strict_json(private_read(path, limit=1024 * 1024))
    require(isinstance(document, dict) and set(document) == {"schema_version", "owners"}
            and document["schema_version"] == "1.0" and isinstance(document["owners"], dict)
            and 1 <= len(document["owners"]) <= 10000, "Mapping must contain schema_version 1.0 and one to 10,000 explicit key/user UUID pairs.")
    try:
        owners = {UUID(key): UUID(value) for key, value in document["owners"].items()}
    except (ValueError, TypeError, AttributeError):
        raise MigrationRefused("Every mapping key and value must be a UUID string.") from None
    require(len(owners) == len(document["owners"]), "Mapping contains repeated representations of the same key UUID.")
    return owners


def provenance_exists(key):
    """Recorded human provenance must not be overwritten by this recovery path."""
    from compute_broker.control.models import AuditEvent
    events = AuditEvent.objects.filter(project_id=key.project_id, action__in=["credential.created", "credential.rotated"])
    for event in events:
        relevant = event.action == "credential.created" and event.object_id == str(key.id)
        if event.action == "credential.rotated" and isinstance(event.details, dict):
            try:
                relevant = UUID(str(event.details.get("replacement_id"))) == key.id
            except (ValueError, TypeError, AttributeError):
                relevant = False
        if relevant:
            try:
                UUID(event.actor)
            except (ValueError, TypeError, AttributeError):
                continue
            return True
    return False


def apply_mapping(owners, mapping_sha256):
    from django.db import connection, transaction
    from compute_broker.control.auth import human_permission
    from compute_broker.control.models import AuditEvent, Membership, ProjectCredential, User
    from compute_broker.control.services import audit
    from compute_broker.control.errors import BrokerError
    from compute_broker.control.forms import ALL_SCOPES
    from compute_broker.extensions import metadata_items

    require(connection.vendor in {"sqlite", "postgresql"}, "Owner verification supports only configured SQLite or PostgreSQL databases.")
    models = model_inventory()
    check_schema(models)
    migrations()
    known_scopes = set(ALL_SCOPES) | {scope for scope, _ in metadata_items("KEY_SCOPES")}
    with transaction.atomic():
        if connection.vendor == "postgresql":
            tables = sorted({model._meta.db_table for model in models} | {"django_migrations"})
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '10s'")
                cursor.execute("LOCK TABLE " + ", ".join(connection.ops.quote_name(name) for name in tables) + " IN ACCESS EXCLUSIVE MODE")
        quiescent_and_balanced()
        planned = []
        for identity in sorted(owners, key=str):
            key = ProjectCredential.objects.select_for_update(of=("self",)).select_related("project").filter(id=identity).first()
            require(key is not None, "Mapping references a missing key.")
            require(key.issued_by_id is None and key.revoked, "Only revoked keys with an unresolved issuing user may be verified.")
            events = list(AuditEvent.objects.filter(project_id=key.project_id, object_id=str(key.id), action="credential.owner_unresolved"))
            require(len(events) == 1 and isinstance(events[0].details, dict)
                    and type(events[0].details.get("was_revoked")) is bool,
                    "Key must have exactly one valid migration quarantine record.")
            quarantine = events[0]
            require(not AuditEvent.objects.filter(project_id=key.project_id, object_id=str(key.id),
                    action="credential.legacy_owner_verified").exists(), "This key's owner was already verified.")
            require(not provenance_exists(key), "Recorded creation/rotation provenance exists; do not overwrite it using an unresolved-owner mapping.")
            require(not AuditEvent.objects.filter(project_id=key.project_id, object_id=str(key.id), action="credential.revoked",
                    created_at__gte=quarantine.created_at).exists(), "An explicit revocation after quarantine must not be undone by owner verification.")
            user = User.objects.select_for_update().filter(id=owners[identity], is_active=True).first()
            require(user is not None, "Mapping references a missing or disabled user.")
            require(user.id == key.project.owner_id or Membership.objects.filter(project=key.project, user=user).exists(),
                    "The verified user must own this project or hold an explicit current project membership.")
            require(isinstance(key.scopes, list) and set(key.scopes) <= known_scopes, "Key scopes are unknown; review them separately before owner verification.")
            try:
                human_permission(user, key.project, "viewer")
                for scope in key.scopes:
                    human_permission(user, key.project, "viewer" if scope in {"jobs:read", "artifacts:read"} else "submitter")
            except BrokerError:
                raise MigrationRefused("The verified user lacks the current project roles required by this key.") from None
            planned.append((key, user, quarantine.details["was_revoked"]))
        # Validate the full batch before changing any owner or revocation state.
        for key, user, was_revoked in planned:
            key.issued_by = user
            key.revoked = was_revoked
            key.save(update_fields=["issued_by", "revoked"])
            audit(key.project, "offline-owner-verification", "credential.legacy_owner_verified", key.id,
                {"issued_by_id": str(user.id), "originally_revoked": was_revoked, "mapping_sha256": mapping_sha256})
        quiescent_and_balanced()
    return {"verified_keys": len(planned), "kept_revoked": sum(was_revoked for _, _, was_revoked in planned),
            "mapping_sha256": mapping_sha256}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--settings", default="compute_broker.control.settings")
    parser.add_argument("--offline-confirmed", action="store_true", required=True,
        help="Confirm all controllers/workers/writers stopped, every project paused, and each mapping manually verified")
    args = parser.parse_args(argv)
    try:
        output_path(args.report)
        owners = load_mapping(args.mapping)
        os.environ["DJANGO_SETTINGS_MODULE"] = args.settings
        import django
        django.setup()
        fingerprint = digest({str(key): str(value) for key, value in owners.items()})
        result = apply_mapping(owners, fingerprint)
        result["report_written"] = write_committed_report(args.report, result)
        print(json.dumps({"ok": True, **result}))
        return 0
    except MigrationRefused as exc:
        print(json.dumps({"ok": False, "error": "OWNER_MAPPING_REFUSED", "message": str(exc)}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"ok": False, "error": "OWNER_MAPPING_FAILED", "message": "Owner verification failed; database details and record contents are not logged."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
