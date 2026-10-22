"""Offline, fail-closed database transfer; never changes the live service config.

Run --help before use. Exports contain password hashes, session data and private
metadata and must receive the same protection as the database backup itself.
"""

from __future__ import annotations

import argparse
from datetime import datetime, time
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
FORMAT = "compute-broker-offline-database-v1"
MAX_BUNDLE_BYTES = 512 * 1024 * 1024


class MigrationRefused(Exception):
    """Messages are fixed operator guidance, never SQL, records or credentials."""


def require(condition, message):
    if not condition:
        raise MigrationRefused(message)


def canonical(value):
    def normalized(item):
        # JSONB may normalize negative zero; JSON object ordering is immaterial.
        # Keep exact integers (including database BigInteger values) unchanged.
        if isinstance(item, float) and item == 0:
            return 0
        if isinstance(item, dict):
            return {key: normalized(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalized(child) for child in item]
        return item
    return json.dumps(normalized(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def private_read(path, *, limit=MAX_BUNDLE_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not stat.S_IMODE(info.st_mode) & 0o077, "Input must be a private regular file owned by this operator.")
        require(info.st_size <= limit, "Input exceeds the bounded migration size limit.")
        data = stream.read(limit + 1)
    require(len(data) <= limit, "Input exceeds the bounded migration size limit.")
    return data


def output_path(path):
    path = Path(path).absolute()
    parent = path.parent.resolve(strict=True)
    temporary = Path(tempfile.gettempdir()).resolve()
    local = (ROOT / "local").resolve()
    require(parent == temporary or temporary in parent.parents or parent == local or local in parent.parents,
            "Outputs must be under the ignored checkout local/ directory or a private temporary directory.")
    require(not path.exists() and not path.is_symlink(), "Output already exists; choose a new path.")
    require(parent.stat().st_uid == os.getuid() and not stat.S_IMODE(parent.stat().st_mode) & 0o077,
            "Output directory must be private (0700) and owned by this operator.")
    return parent / path.name


def private_write(path, data):
    path = output_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def file_digest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def secret_value(path):
    value = private_read(path, limit=65536).decode("utf-8").strip()
    require(len(value) >= 32, "The preserved Django secret is missing or invalid.")
    return value


def configure_sqlite(path, extensions, secret, *, immutable):
    import django
    from django.conf import settings
    from compute_broker.extensions import application_configs, configured_names

    names = configured_names(extensions)
    database = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path),
                "OPTIONS": {"timeout": 15, "transaction_mode": "IMMEDIATE"}}
    if immutable:
        database["NAME"] = "file:" + quote(str(Path(path).absolute()), safe="/") + "?mode=ro&immutable=1"
        database["OPTIONS"] = {"uri": True, "transaction_mode": "DEFERRED"}
    settings.configure(INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
        "compute_broker.control.apps.BrokerConfig", *application_configs(names)],
        AUTH_USER_MODEL="broker.User", DATABASES={"default": database}, SECRET_KEY=secret,
        BROKER_EXTENSIONS=names, USE_TZ=True, TIME_ZONE="UTC", DEFAULT_AUTO_FIELD="django.db.models.BigAutoField")
    django.setup()


def model_inventory():
    from django.apps import apps
    return sorted((model for model in apps.get_models(include_auto_created=True)
                   if model._meta.managed and not model._meta.proxy), key=lambda model: model._meta.label_lower)


def migrations():
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader
    from django.db.migrations.recorder import MigrationRecorder

    loader = MigrationLoader(connection)
    applied = set(loader.applied_migrations)
    require(applied == set(loader.disk_migrations), "Source and target must have every current installed migration applied, with no unknown migrations.")
    signature = []
    for key in sorted(applied):
        module = sys.modules[loader.disk_migrations[key].__module__]
        signature.append({"app": key[0], "name": key[1], "sha256": file_digest(module.__file__)})
    rows = list(MigrationRecorder(connection).migration_qs.order_by("id").values())
    return signature, rows


def exact_encoder():
    from django.core.serializers.json import DjangoJSONEncoder

    class Encoder(DjangoJSONEncoder):
        def default(self, value):
            # Django's default JSON encoder truncates datetime microseconds.
            if isinstance(value, datetime):
                return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
            if isinstance(value, time):
                require(value.tzinfo is None, "Timezone-aware time fields are unsupported.")
                return value.isoformat(timespec="microseconds")
            return super().default(value)
    return Encoder


def fixture_rows(models):
    from django.core.serializers import sort_dependencies
    from django.core.serializers.json import Serializer

    class ExactSerializer(Serializer):
        def handle_m2m_field(self, obj, field):
            # Serialize the actual through rows, preserving their PKs and gaps.
            pass

    from django.apps import apps
    ordered = sort_dependencies([(config, [model for model in models if model._meta.app_config == config])
                                 for config in apps.get_app_configs()], allow_cycles=True)
    objects = (obj for model in ordered for obj in model._base_manager.order_by(model._meta.pk.name).iterator())
    text = ExactSerializer().serialize(objects, use_natural_foreign_keys=True, use_natural_primary_keys=False,
                                       cls=exact_encoder(), ensure_ascii=False, allow_nan=False)
    return json.loads(text)


def manifest_for(rows, migration_rows, models):
    groups = {model._meta.label_lower: [] for model in models}
    for row in rows:
        require(row["model"] in groups, "Bundle contains an unknown model.")
        groups[row["model"]].append(row)
    tables = {}
    for model in models:
        records = sorted(groups[model._meta.label_lower], key=lambda row: canonical(row["pk"]))
        tables[model._meta.db_table] = {"model": model._meta.label_lower, "count": len(records), "sha256": digest(records),
            "columns": [field.column for field in model._meta.local_fields]}
    normalized = json.loads(json.dumps(migration_rows, cls=exact_encoder()))
    tables["django_migrations"] = {"model": "migrations.migration", "count": len(normalized),
                                   "sha256": digest(normalized), "columns": ["id", "app", "name", "applied"]}
    return tables


def check_schema(models):
    from django.db import connection
    actual = set(connection.introspection.table_names())
    expected = {model._meta.db_table for model in models} | {"django_migrations"}
    require(actual == expected, "Database tables do not match the enabled applications; enable every source module and use an empty migrated target.")
    with connection.cursor() as cursor:
        for model in models:
            columns = {column.name for column in connection.introspection.get_table_description(cursor, model._meta.db_table)}
            require(columns == {field.column for field in model._meta.local_fields}, "Database columns do not match this code revision.")


def quiescent_and_balanced():
    from django.apps import apps
    from django.db.models import Sum
    from django.utils import timezone
    from compute_broker.control.models import (Attempt, Blob, Connection, ExecutionRequest, Grant,
        LedgerEntry, Project, StoragePool, UploadReservation)

    require(not Project.objects.filter(paused=False).exists(), "Pause every project and stop controllers, workers and writers before taking the backup.")
    require(not Attempt.objects.exclude(state__in=["succeeded", "failed", "cancelled", "expired"]).exists(),
            "Nonterminal or ambiguous attempts must be reconciled before migration; there is no unsafe override.")
    require(not Attempt.objects.exclude(accounting_state__in=["settled", "released"]).exists(), "Every attempt must have final settled or released accounting.")
    require(not ExecutionRequest.objects.filter(state="awaiting_approval").exists(), "Resolve pending execution approvals before migration.")
    require(not Attempt.objects.filter(lease_expires_at__gt=timezone.now()).exists(), "Wait for active execution leases to drain before migration.")
    require(not Connection.objects.filter(discovery_state__in=["queued", "discovering"]).exists(), "Finish or safely resolve queued/in-flight discovery before migration.")
    require(not UploadReservation.objects.exclude(state__in=["finalized", "aborted"]).exists(), "Finish or safely repair pending upload reservations before migration.")
    require(not LedgerEntry.objects.exclude(action__in=["reserve", "settle", "release"]).exists(), "Unknown ledger actions require an explicit migration implementation.")
    for attempt in Attempt.objects.all():
        entries = list(attempt.ledgerentry_set.all())
        reserve = [item for item in entries if item.action == "reserve"]
        final = [item for item in entries if item.action == ("settle" if attempt.accounting_state == "settled" else "release")]
        require(len(entries) == 2 and len(reserve) == len(final) == 1
                and (reserve[0].cpu, reserve[0].gpu) == (attempt.reservation_cpu, attempt.reservation_gpu)
                and all(item.grant_id == attempt.grant_id for item in entries)
                and min(final[0].cpu, final[0].gpu) >= 0
                and (attempt.accounting_state != "released" or (final[0].cpu, final[0].gpu) == (0, 0)),
                "Attempt reservations and final ledger entries are inconsistent.")
    balances = []
    for grant in Grant.objects.order_by("id"):
        settled = grant.entries.filter(action="settle").aggregate(cpu=Sum("cpu"), gpu=Sum("gpu"))
        require(grant.reserved_cpu == grant.reserved_gpu == 0
                and grant.spent_cpu == (settled["cpu"] or 0) and grant.spent_gpu == (settled["gpu"] or 0),
                "Stored compute balances do not match the settled ledger.")
        balances.append({"grant": str(grant.id), "spent_cpu": grant.spent_cpu, "spent_gpu": grant.spent_gpu,
                         "reserved_cpu": grant.reserved_cpu, "reserved_gpu": grant.reserved_gpu})
    total = 0
    for project in Project.objects.all():
        blobs = Blob.objects.filter(project=project)
        size = blobs.aggregate(size=Sum("size_bytes"))["size"] or 0
        require(project.storage_reserved == 0 and project.storage_used == size and project.object_count == blobs.count(),
                "Project storage counters require repair before migration.")
        total += size
    pools = list(StoragePool.objects.all())
    require((not pools and total == 0) or len(pools) == 1 and pools[0].id == 1
            and pools[0].reserved == 0 and pools[0].used == total, "Global storage counters require repair before migration.")
    if apps.is_installed("compute_broker.extensions.workflows"):
        from compute_broker.extensions.workflows.models import WorkflowRun, WorkflowNodeRun
        require(not WorkflowRun.objects.exclude(state__in=["succeeded", "failed", "cancelled"]).exists()
                and not WorkflowNodeRun.objects.exclude(state__in=["succeeded", "failed", "cancelled", "rejected", "expired", "blocked_upstream", "blocked_artifact"]).exists(),
                "Finish or cancel every active workflow before migration.")
        require(not WorkflowNodeRun.objects.filter(claim_expires_at__gt=timezone.now()).exists(), "Workflow claims must drain before migration.")
    if apps.is_installed("compute_broker.extensions.environments"):
        from compute_broker.extensions.environments.models import EnvironmentBuild
        require(not EnvironmentBuild.objects.exclude(state__in=["draft", "prepared", "ready", "failed", "cancelled", "expired", "simulation_only", "finalization_failed"]).exists(),
                "Finish environment build execution/accounting/finalization before migration.")
    if apps.is_installed("compute_broker.extensions.storage"):
        from compute_broker.extensions.storage.models import StorageObservationLease
        require(not StorageObservationLease.objects.filter(state="leased").exists(), "Finish or safely expire active storage observations before migration.")
    return balances


def verify_artifacts(root):
    from compute_broker.control.models import Blob
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for blob in Blob.objects.all():
            require(re.fullmatch(r"[0-9a-f]{64}", blob.sha256) is not None, "Stored artifact digest is malformed.")
            project_fd = os.open(str(blob.project_id), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                fd = os.open(blob.sha256, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=project_fd)
            finally:
                os.close(project_fd)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_size == blob.size_bytes
                        and hashlib.file_digest(stream, "sha256").hexdigest() == blob.sha256,
                        "A retained artifact is missing or failed its size/digest check.")
    finally:
        os.close(root_fd)


def export_database(args):
    import django
    path = Path(args.sqlite_backup).absolute()
    # Validate permissions without reading a potentially large snapshot into RAM.
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o077,
            "SQLite backup must be a private, non-symlink regular file owned by this operator.")
    require(not any(Path(str(path) + suffix).exists() for suffix in ["-wal", "-shm", "-journal"]),
            "Use a standalone SQLite backup without WAL, SHM or journal sidecars; never mark a live database immutable.")
    source_hash = file_digest(path)
    secret = secret_value(args.secret_key_file)
    configure_sqlite(path, args.extensions, secret, immutable=True)
    from django.conf import settings
    from django.db import connection
    with connection.cursor() as cursor:
        require(cursor.execute("PRAGMA quick_check").fetchall() == [("ok",)], "SQLite integrity check failed.")
        require(not cursor.execute("PRAGMA foreign_key_check").fetchall(), "SQLite foreign-key check failed.")
    models = model_inventory()
    check_schema(models)
    signature, history = migrations()
    balances = quiescent_and_balanced()
    verify_artifacts(args.artifact_root)
    rows = fixture_rows(models)
    require(file_digest(path) == source_hash, "Source snapshot changed while exporting; stop and take a new offline backup.")
    bundle = {"format": FORMAT, "django_version": django.get_version(), "extensions": list(settings.BROKER_EXTENSIONS),
        "source_sha256": source_hash, "secret_sha256": hashlib.sha256(secret.encode()).hexdigest(),
        "migration_signature": signature, "migration_rows": json.loads(json.dumps(history, cls=exact_encoder())),
        "records": rows, "tables": manifest_for(rows, history, models), "balances": balances}
    bundle["bundle_sha256"] = digest(bundle)
    encoded = canonical(bundle)
    require(len(encoded) + 1 <= MAX_BUNDLE_BYTES, "Export exceeds the bounded in-memory migration size limit.")
    private_write(args.output, encoded + b"\n")
    return {"operation": "export", "tables": len(bundle["tables"]), "records": len(rows), "bundle_sha256": bundle["bundle_sha256"]}


def load_bundle(path):
    from compute_broker.domain.validation import strict_json
    bundle = strict_json(private_read(path))
    require(isinstance(bundle, dict) and bundle.get("format") == FORMAT, "Unsupported export format.")
    checksum = bundle.pop("bundle_sha256", None)
    require(checksum == digest(bundle), "Export checksum failed; do not import modified or truncated data.")
    bundle["bundle_sha256"] = checksum
    return bundle


def target_matches(bundle, secret):
    import django
    from django.conf import settings
    require(django.get_version() == bundle["django_version"], "Use the exact same Django version for source and target.")
    require(list(settings.BROKER_EXTENSIONS) == bundle["extensions"], "Enable exactly the source extensions, in the same order.")
    require(hashlib.sha256(secret.encode()).hexdigest() == bundle["secret_sha256"], "Preserve the original Django secret file; sessions must not be silently invalidated.")
    require(settings.SECRET_KEY == secret, "Configured target Django secret differs from the preserved secret file.")
    models = model_inventory()
    check_schema(models)
    signature, _ = migrations()
    require(signature == bundle["migration_signature"], "Code/migration signature differs from the exported source.")
    require(manifest_for(bundle["records"], bundle["migration_rows"], models) == bundle["tables"], "Export table manifest does not match its records.")
    return models


def bootstrap_only(models):
    from django.contrib.contenttypes.models import ContentType
    from django.contrib.auth.models import Permission
    expected = {(model._meta.app_label, model._meta.model_name) for model in models if not model._meta.auto_created}
    require(set(ContentType.objects.values_list("app_label", "model")) == expected,
            "Target content types are not a fresh bootstrap of these applications.")
    permission_keys = {(model._meta.app_label, model._meta.model_name, action + "_" + model._meta.model_name)
                       for model in models if not model._meta.auto_created for action in model._meta.default_permissions}
    permission_keys |= {(model._meta.app_label, model._meta.model_name, code)
                        for model in models if not model._meta.auto_created for code, _ in model._meta.permissions}
    require(set(Permission.objects.values_list("content_type__app_label", "content_type__model", "codename")) == permission_keys,
            "Target permissions are not a fresh bootstrap of these applications.")
    for model in models:
        if model not in (ContentType, Permission):
            require(not model._base_manager.exists(), "Target contains data; import only into a new, migrated database.")


def verify_database(bundle, models, artifact_root):
    _, history = migrations()
    require(manifest_for(fixture_rows(models), history, models) == bundle["tables"], "Post-import table count/digest verification failed; transaction will roll back.")
    require(quiescent_and_balanced() == bundle["balances"], "Post-import balance verification failed.")
    verify_artifacts(artifact_root)


def import_database(bundle, args, *, rehearsal=False):
    from django.core import serializers
    from django.core.management.color import no_style
    from django.db import connection, transaction
    from django.db.migrations.recorder import MigrationRecorder

    require(connection.vendor == ("sqlite" if rehearsal else "postgresql"), "Import requires PostgreSQL; SQLite is allowed only through the new-file rehearsal command.")
    if not rehearsal:
        from django.conf import settings
        require(Path(settings.ARTIFACT_ROOT).resolve() == Path(args.artifact_root).resolve(),
                "Configured target artifact root differs from the verified artifact root.")
    secret = secret_value(args.secret_key_file)
    models = target_matches(bundle, secret)
    with transaction.atomic():
        if connection.vendor == "postgresql":
            # Hold all known data tables against concurrent inserts until the
            # empty-target check, restore and verification have committed.
            tables = sorted(bundle["tables"])
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '10s'")
                cursor.execute("LOCK TABLE " + ", ".join(connection.ops.quote_name(name) for name in tables) + " IN ACCESS EXCLUSIVE MODE")
        bootstrap_only(models)
        with connection.constraint_checks_disabled():
            with connection.cursor() as cursor:
                for table in sorted(bundle["tables"]):
                    cursor.execute("DELETE FROM " + connection.ops.quote_name(table))
            deferred = []
            for obj in serializers.deserialize("json", json.dumps(bundle["records"]), handle_forward_references=True):
                obj.save()
                if obj.deferred_fields:
                    deferred.append(obj)
            for obj in deferred:
                obj.save_deferred_fields()
            recorder = MigrationRecorder(connection)
            migration_model = recorder.Migration
            from django.utils.dateparse import parse_datetime
            migration_model.objects.bulk_create([migration_model(**{**row, "applied": parse_datetime(row["applied"])}) for row in bundle["migration_rows"]])
            connection.check_constraints()
        verify_database(bundle, models, args.artifact_root)
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), [*models, migration_model]):
                cursor.execute(sql)
    return {"operation": "rehearsal" if rehearsal else "import", "tables": len(bundle["tables"]),
            "records": len(bundle["records"]), "bundle_sha256": bundle["bundle_sha256"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Read an offline immutable SQLite backup and retained artifacts")
    export.add_argument("--sqlite-backup", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--extensions", default=os.environ.get("BROKER_EXTENSIONS", ""))
    for name in ["import", "verify", "rehearse-sqlite"]:
        command = commands.add_parser(name)
        command.add_argument("--input", required=True)
        command.add_argument("--report", required=True)
        if name == "rehearse-sqlite":
            command.add_argument("--output-db", required=True)
        else:
            command.add_argument("--settings", default="compute_broker.control.settings")
    for command in [export, *[commands.choices[name] for name in ["import", "verify", "rehearse-sqlite"]]]:
        command.add_argument("--secret-key-file", required=True)
        command.add_argument("--artifact-root", required=True)
        command.add_argument("--offline-confirmed", action="store_true", required=True,
                             help="Confirm all controllers/workers/writers stopped, every project paused, and only one database will resume")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            output_path(args.output)
            result = export_database(args)
        else:
            output_path(args.report)
            bundle = load_bundle(args.input)
            secret = secret_value(args.secret_key_file)
            if args.command == "rehearse-sqlite":
                database = output_path(args.output_db)
                private_write(database, b"")
                configure_sqlite(database, ",".join(bundle["extensions"]), secret, immutable=False)
                from django.core.management import call_command
                call_command("migrate", interactive=False, verbosity=0)
            else:
                os.environ["DJANGO_SETTINGS_MODULE"] = args.settings
                import django
                django.setup()
            if args.command == "verify":
                from django.db import connection, transaction
                require(connection.vendor == "postgresql", "Verification target must be PostgreSQL.")
                from django.conf import settings
                require(Path(settings.ARTIFACT_ROOT).resolve() == Path(args.artifact_root).resolve(),
                        "Configured target artifact root differs from the verified artifact root.")
                models = target_matches(bundle, secret)
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    verify_database(bundle, models, args.artifact_root)
                result = {"operation": "verify", "tables": len(bundle["tables"]), "records": len(bundle["records"]), "bundle_sha256": bundle["bundle_sha256"]}
            else:
                result = import_database(bundle, args, rehearsal=args.command == "rehearse-sqlite")
            private_write(args.report, canonical({"ok": True, **result}) + b"\n")
        print(json.dumps({"ok": True, **result}))
        return 0
    except MigrationRefused as exc:
        print(json.dumps({"ok": False, "error": "MIGRATION_REFUSED", "message": str(exc)}), file=sys.stderr)
        return 1
    except Exception:
        # Database exceptions and malformed records may contain password/SQL
        # values. Never emit them or a traceback into terminal or CI logs.
        print(json.dumps({"ok": False, "error": "MIGRATION_FAILED", "message": "Migration failed safely; no database details or record contents are logged. Check private inputs and prerequisites."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
