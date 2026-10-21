"""Small Django-free contract for optional fixed worker maintenance."""

import re
from uuid import UUID

from .planning import SAFE_INTEGER


def probe_context(lease):
    if (not isinstance(lease, dict) or set(lease) != {"id", "generation", "configuration", "context", "operation", "extension"}
            or lease.get("operation") != "extension" or lease.get("extension") != "storage"
            or type(lease.get("generation")) is not int or not 1 <= lease["generation"] <= SAFE_INTEGER):
        raise ValueError("Invalid storage maintenance lease")
    if str(UUID(lease["id"])) != lease["id"]:
        raise ValueError("Noncanonical storage lease identity")
    context = lease["context"]
    fields = {"pool_id", "target_id", "connection_id", "root_alias", "profile_revision_sha256", "connection_revision_sha256"}
    if not isinstance(context, dict) or set(context) != fields:
        raise ValueError("Invalid storage maintenance context")
    for key in ("pool_id", "target_id", "connection_id"):
        if not isinstance(context[key], str) or str(UUID(context[key])) != context[key]:
            raise ValueError("Invalid storage target identity")
    if not isinstance(context["root_alias"], str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}", context["root_alias"]):
        raise ValueError("Invalid protected storage alias")
    for key in ("profile_revision_sha256", "connection_revision_sha256"):
        if not isinstance(context[key], str) or not re.fullmatch(r"[0-9a-f]{64}", context[key]):
            raise ValueError("Invalid protected storage binding")
    return context
