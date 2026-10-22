"""Protected provider configuration, validated without network operations."""

import copy
import hashlib
import os
import re
import stat
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
import rfc8785

from compute_broker.domain.validation import strict_json

DEFAULT_PROVIDERS = {"local": {"kind": "local", "label": "Local account", "enabled": True}}
_ATTRIBUTE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
_SECRET_ATTRIBUTES = {"userpassword", "authpassword", "unicodepwd", "sambantpassword", "sambalmpassword", "krbprincipalkey", "pwdhistory"}


def _text(value, maximum, *, allow_empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid provider configuration value")
    return value


def _path(value):
    value = _text(value, 4096)
    if not value.startswith("/") or value == "/" or os.path.normpath(value) != value:
        raise ValueError("Provider paths must be explicit absolute paths")
    return value


def validate_provider_configuration(document):
    if not isinstance(document, dict) or not 1 <= len(document) <= 8 or "local" not in document:
        raise ValueError("Provider configuration must explicitly include local and at most eight providers")
    result = {}
    common = {"kind", "label", "enabled"}
    required = {"server_uri", "start_tls", "ca_cert_file", "bind_dn", "bind_password_file", "user_base_dn"}
    optional = {"username_attribute", "subject_attribute", "object_class", "network_timeout_seconds", "operation_timeout_seconds", "freshness_seconds", "profile_attributes", "status_policy"}
    for identifier, value in document.items():
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", identifier) or not isinstance(value, dict):
            raise ValueError("Invalid provider identifier")
        if not common <= value.keys() or type(value["enabled"]) is not bool:
            raise ValueError("Provider needs kind, label and explicit enabled state")
        _text(value["label"], 80)
        if identifier == "local":
            if value["kind"] != "local" or set(value) != common:
                raise ValueError("Local provider has no directory settings")
            result[identifier] = copy.deepcopy(value)
            continue
        if value["kind"] != "ldap" or not required <= value.keys() or set(value) - common - required - optional:
            raise ValueError("Invalid LDAP provider fields; inline passwords are prohibited")
        normalized = copy.deepcopy(value)
        uri = _text(value["server_uri"], 1024)
        parsed = urlsplit(uri)
        if (parsed.scheme not in {"ldap", "ldaps"} or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment or any(c.isspace() for c in uri)):
            raise ValueError("Configure exactly one LDAP server URI without credentials or a path")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", parsed.hostname) or parsed.hostname.startswith("-"):
            raise ValueError("Invalid LDAP host")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid LDAP port")
        if type(value["start_tls"]) is not bool or value["start_tls"] != (parsed.scheme == "ldap"):
            raise ValueError("Use LDAPS or mandatory StartTLS; cleartext and TLS downgrade are prohibited")
        for key in ("ca_cert_file", "bind_password_file"):
            _path(value[key])
        for key in ("bind_dn", "user_base_dn"):
            _text(value[key], 2048)
        for key, default in (("username_attribute", "uid"), ("subject_attribute", "entryUUID"), ("object_class", "inetOrgPerson")):
            normalized[key] = value.get(key, default)
            if not isinstance(normalized[key], str) or not _ATTRIBUTE.fullmatch(normalized[key]):
                raise ValueError("Invalid directory attribute or object class")
        if normalized["subject_attribute"].lower() != "entryuuid":
            raise ValueError("This adapter supports only immutable OpenLDAP entryUUID subjects")
        if normalized["username_attribute"].lower() in _SECRET_ATTRIBUTES:
            raise ValueError("Credential attributes cannot identify a user")
        for key, default, maximum in (("network_timeout_seconds", 5, 10), ("operation_timeout_seconds", 5, 10), ("freshness_seconds", 300, 300)):
            normalized[key] = value.get(key, default)
            if type(normalized[key]) is not int or not 1 <= normalized[key] <= maximum:
                raise ValueError("Directory timeouts and freshness must have finite bounds")
        mapping = value.get("profile_attributes", {"first_name": "givenName", "last_name": "sn", "email": "mail", "display_name": "displayName"})
        if not isinstance(mapping, dict) or set(mapping) - {"first_name", "last_name", "email", "display_name"}:
            raise ValueError("Unsupported directory profile mapping")
        if any(not isinstance(v, str) or not _ATTRIBUTE.fullmatch(v) for v in mapping.values()):
            raise ValueError("Invalid directory profile attribute")
        if any(v.lower() in _SECRET_ATTRIBUTES for v in mapping.values()):
            raise ValueError("Credential attributes cannot populate a user profile")
        normalized["profile_attributes"] = copy.deepcopy(mapping)
        policy = value.get("status_policy", {})
        if not isinstance(policy, dict) or set(policy) - {"shadow_expire", "samba_account_flags", "required_group_dn", "group_object_class", "group_membership_attribute", "required_group_subject"}:
            raise ValueError("Invalid explicit directory eligibility policy")
        for key in ("shadow_expire", "samba_account_flags"):
            if key in policy and type(policy[key]) is not bool:
                raise ValueError("Directory status checks need explicit booleans")
        if "required_group_dn" in policy:
            _text(policy["required_group_dn"], 2048)
            for key in ("group_object_class", "group_membership_attribute"):
                if key in policy and (not isinstance(policy[key], str) or not _ATTRIBUTE.fullmatch(policy[key])):
                    raise ValueError("Invalid directory group policy")
            if policy.get("group_membership_attribute", "memberUid").lower() != "memberuid":
                raise ValueError("This adapter supports only POSIX memberUid group membership")
            if "required_group_subject" in policy:
                from uuid import UUID
                if str(UUID(policy["required_group_subject"])) != policy["required_group_subject"]:
                    raise ValueError("Expected canonical group entryUUID")
        elif set(policy) & {"group_object_class", "group_membership_attribute", "required_group_subject"}:
            raise ValueError("Directory group checks require an explicit group DN")
        normalized["status_policy"] = copy.deepcopy(policy)
        result[identifier] = normalized
    return result


def load_provider_configuration(filename=None):
    """Read only the operator-selected secret-free JSON, never scan for config."""
    if not filename:
        return copy.deepcopy(DEFAULT_PROVIDERS)
    try:
        filename = _path(str(filename))
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536 or info.st_mode & 0o022:
                raise ValueError("Provider configuration must be a bounded non-writable regular file")
            data = os.read(fd, 65537)
        finally:
            os.close(fd)
        if len(data) > 65536:
            raise ValueError("Provider configuration exceeds limit")
        return validate_provider_configuration(strict_json(data))
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise ImproperlyConfigured("Identity provider configuration is unavailable or invalid") from None


def providers():
    from django.conf import settings
    try:
        return validate_provider_configuration(getattr(settings, "BROKER_IDENTITY_PROVIDERS", DEFAULT_PROVIDERS))
    except (ValueError, TypeError, KeyError):
        raise ImproperlyConfigured("Identity provider configuration is invalid") from None


def get_login_choices():
    return [(key, value["label"]) for key, value in providers().items() if value["enabled"]]


def provider_fingerprint(configuration):
    return hashlib.sha256(rfc8785.dumps(configuration)).hexdigest()
