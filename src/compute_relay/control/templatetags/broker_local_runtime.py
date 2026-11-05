from django import template
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from compute_relay.local_connectors import LocalConnectorError, loopback_origin

register = template.Library()


@register.simple_tag
def connector_contact(worker):
    from compute_relay.control.worker_status import worker_contact
    return worker_contact(worker.last_seen_at, worker.revoked)


@register.simple_tag
def connector_session(cluster):
    registration = getattr(cluster, "registration", None)
    if registration is None:
        return {}
    runtime = registration.connector_settings.get("runtime", {})
    if runtime.get("worker_id") != str(cluster.worker_id):
        return {}
    expires = parse_datetime(runtime.get("expires_at") or "")
    seconds = (expires - timezone.now()).total_seconds() if expires and timezone.is_aware(expires) else None
    state = runtime.get("state")
    return {"state": state, "expires_at": expires,
        "expiring": state == "connected" and seconds is not None and 0 < seconds <= 300,
        "expired": state == "connected" and seconds is not None and seconds <= 0}


@register.simple_tag
def local_connector_start_available():
    if getattr(settings, "BROKER_LOCAL_RUNTIME", False) not in {True, "1"}:
        return False
    try:
        loopback_origin(getattr(settings, "BROKER_LOCAL_ORIGIN", ""))
    except LocalConnectorError:
        return False
    return True
