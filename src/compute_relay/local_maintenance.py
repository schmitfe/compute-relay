"""Explicit offline maintenance for a stopped personal installation."""

import json
import os
import subprocess
import sys
from uuid import uuid4

from .local_state import AlreadyRunning, LocalState, LocalStateError, default_directory, validate_installation


def run(args):
    from .connector_profiles import _private_file
    from .local_runtime import service_environment

    if not 1 <= args.limit <= 1000 or not 1 <= args.older_than_days <= 36500:
        raise SystemExit("Choose a batch limit from 1 through 1000 and an age from 1 through 36500 days.")
    try:
        with LocalState(args.data_directory or default_directory(), create=False) as state, state.lock():
            document = state.read("installation.json")
            if document is None or not validate_installation(document)["initialized"]:
                raise LocalStateError("Choose an initialized installation.")
            descriptor = _private_file(state.descriptor, "control.sqlite3", os.O_RDONLY)
            os.close(descriptor)
            request = {"apply": args.apply, "archive_drafts": args.archive_drafts,
                "compact_idempotency": args.compact_idempotency,
                "older_than_days": args.older_than_days, "limit": args.limit}
            with state.log("maintenance.log") as log:
                result = subprocess.run([sys.executable, "-I", "-m", "compute_relay.local_maintenance"],
                    input=json.dumps(request).encode(), stdout=subprocess.PIPE, stderr=log,
                    env=service_environment(state, document, str(uuid4())), timeout=120)
            if result.returncode or len(result.stdout) > 65536:
                raise LocalStateError("Maintenance could not finish. See private maintenance.log; use the matching installation version and initialized state.")
            response = json.loads(result.stdout)
            if not isinstance(response, dict):
                raise ValueError
            print(json.dumps(response, sort_keys=True))
    except AlreadyRunning:
        raise SystemExit("Stop Compute Relay and its connectors before maintenance.") from None
    except LocalStateError as error:
        raise SystemExit(f"Compute Relay: {error}") from None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise SystemExit("Maintenance could not finish. Check the stopped installation and its private maintenance.log.") from None


def main():
    from datetime import timedelta
    os.environ["DJANGO_SETTINGS_MODULE"] = "compute_relay.control.settings"
    import django
    django.setup()
    from django.conf import settings
    from django.utils import timezone
    from .control.maintenance import compact_draft_jobs
    from .control.metadata import compact_idempotency, metadata_usage
    from .control.models import IdempotencyRecord, UploadReservation, User
    from .control.storage import reconcile_uploads

    request = json.loads(sys.stdin.buffer.read(4097))
    owner = User.objects.get(id=settings.BROKER_LOCAL_OWNER_ID, is_active=True)
    before = timezone.now() - timedelta(days=request["older_than_days"])
    result = {"applied": request["apply"], "metadata_before": metadata_usage(),
        "expired_uploads": UploadReservation.objects.filter(state="reserved", expires_at__lte=timezone.now()).count()}
    if request["archive_drafts"]:
        result["draft_jobs"] = compact_draft_jobs(owner=owner, before=before,
            limit=request["limit"], apply=request["apply"])
    if request["compact_idempotency"]:
        result["idempotency_candidates"] = min(request["limit"], IdempotencyRecord.objects.filter(
            project__owner=owner, compacted=False, created_at__lt=before).count())
        if request["apply"]:
            result["idempotency_compacted"] = compact_idempotency(before=before, limit=request["limit"])
    if request["apply"]:
        result["upload_recovery"] = reconcile_uploads(limit=request["limit"])
    result["metadata_after"] = metadata_usage()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
