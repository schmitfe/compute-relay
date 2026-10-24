"""Initialize the local database in a child process; never print a password."""

import json
import os
from pathlib import Path
import sys


def main():
    sys.dont_write_bytecode = True
    raw = sys.stdin.buffer.read(16385)
    try:
        document = json.loads(raw)
        if (len(raw) > 16384 or set(document) != {"set_password", "password", "initialized"}
                or type(document["set_password"]) is not bool or type(document["initialized"]) is not bool
                or document["password"] is not None and (not isinstance(document["password"], str)
                    or not 1 <= len(document["password"]) <= 4096 or "\x00" in document["password"])):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise SystemExit("Invalid local initialization request.") from None
    os.environ["DJANGO_SETTINGS_MODULE"] = "compute_relay.control.settings"
    import django
    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connection, transaction
    from compute_relay.control.models import User

    call_command("migrate", interactive=False, verbosity=0)
    if connection.vendor != "sqlite":
        raise SystemExit("The local application requires its embedded SQLite database.")
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL")
        if cursor.fetchone()[0].lower() != "wal":
            raise SystemExit("Could not enable durable local database concurrency.")
    with transaction.atomic():
        if User.objects.exclude(pk=settings.BROKER_LOCAL_OWNER_ID).filter(is_active=True).exists():
            raise SystemExit("This database contains other active accounts. Use a dedicated single-user installation.")
        owner = User.objects.filter(pk=settings.BROKER_LOCAL_OWNER_ID).first()
        if document["initialized"] and owner is None:
            raise SystemExit("The saved local owner is missing; restore the installation backup.")
        if owner is None:
            owner = User(id=settings.BROKER_LOCAL_OWNER_ID, username="local", is_active=True)
            if not document["set_password"]:
                raise SystemExit("The first local launch must configure portal access.")
        elif not owner.is_active or owner.username != "local":
            raise SystemExit("The saved local owner is disabled or has unexpected identity metadata.")
        if document["set_password"]:
            if settings.BROKER_LOCAL_AUTH_MODE == "none":
                if document["password"] is not None:
                    raise SystemExit("Password-free setup does not accept a password.")
                owner.set_unusable_password()
            else:
                if document["password"] is None:
                    raise SystemExit("A portal password is required for password mode.")
                owner.set_password(document["password"])
            owner.save()
        elif owner.has_usable_password() != (settings.BROKER_LOCAL_AUTH_MODE == "password"):
            raise SystemExit("Portal access configuration is incomplete. Repeat the explicit password setup option.")
    # A stopped uploader can leave a durable reservation or published bytes.
    # Recover only after validating the saved owner. The filesystem fence keeps
    # live writers safe, and unavailable objects remain reserved for inspection.
    from compute_relay.control.storage import reconcile_uploads
    reconcile_uploads(limit=32)
    connection.close()
    database = Path(settings.DATABASES["default"]["NAME"])
    database.chmod(0o600)


if __name__ == "__main__":
    main()
