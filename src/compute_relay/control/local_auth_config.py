"""Validate local authentication settings without importing Django models."""

import ipaddress
from urllib.parse import urlsplit
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured


def configured_owner(value, *, name="BROKER_LOCAL_OWNER_ID"):
    if value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ImproperlyConfigured(f"{name} must be a UUID") from None


def local_auth_configuration(environ):
    owner = configured_owner(environ.get("BROKER_LOCAL_OWNER_ID"))
    mode = environ.get("BROKER_LOCAL_AUTH_MODE", "password")
    if mode not in {"password", "none"}:
        raise ImproperlyConfigured("BROKER_LOCAL_AUTH_MODE must be password or none")
    origin = environ.get("BROKER_LOCAL_ORIGIN", "")
    if mode == "none":
        if environ.get("BROKER_LOCAL_RUNTIME") != "1" or owner is None:
            raise ImproperlyConfigured("Password-free access requires the local runtime and its owner")
        try:
            parts = urlsplit(origin)
            host = parts.hostname
            local = host == "localhost" or ipaddress.ip_address(host).is_loopback
            valid = (parts.scheme == "http" and local and parts.port is not None and parts.port > 0
                and not parts.username and not parts.password and parts.path in {"", "/"}
                and not parts.query and not parts.fragment and parts.netloc == origin.removeprefix("http://").rstrip("/"))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ImproperlyConfigured("Password-free access requires an exact HTTP loopback origin with a port")
        origin = "http://" + parts.netloc
    return owner, mode, origin
