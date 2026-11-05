from django import template
from django.conf import settings

from compute_relay.local_connectors import LocalConnectorError, loopback_origin

register = template.Library()


@register.simple_tag
def connector_contact(worker):
    from compute_relay.control.worker_status import worker_contact
    return worker_contact(worker.last_seen_at, worker.revoked)


@register.simple_tag
def local_connector_start_available():
    if getattr(settings, "BROKER_LOCAL_RUNTIME", False) not in {True, "1"}:
        return False
    try:
        loopback_origin(getattr(settings, "BROKER_LOCAL_ORIGIN", ""))
    except LocalConnectorError:
        return False
    return True
