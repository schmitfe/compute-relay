"""Validation shared by the local MCP and workspace clients."""

import re
from urllib.parse import urlsplit


def broker_url(value):
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Use a credential-free HTTPS broker origin, or HTTP on loopback.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Use a broker origin with a valid hostname and port.") from None
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and not loopback)
            or port == 0):
        raise ValueError("Use a credential-free HTTPS broker origin, or HTTP on loopback.")
    return value.rstrip("/")


def validate_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"cbp_[A-Za-z0-9_-]{16,200}", value):
        raise ValueError("Enter a project API key created in the broker portal, without a Bearer prefix.")
    return value
