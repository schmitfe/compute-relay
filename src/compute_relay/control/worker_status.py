"""Saved contact freshness, not proof of SSH reachability or native job state."""

from datetime import timedelta

from django.utils import timezone


def worker_contact(last_seen_at, revoked=False, *, now=None):
    if revoked:
        status = "revoked"
    elif last_seen_at is None:
        status = "not_seen"
    elif (now or timezone.now()) - last_seen_at > timedelta(minutes=5):
        status = "stale"
    else:
        status = "recent"
    return {"status": status, "last_contact_at": last_seen_at.isoformat() if last_seen_at else None}
