"""Cached authority checks never perform network work inside a ledger lock."""

from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from .ldap_adapter import DirectoryFailure, LDAPAdapter
from .models import AuthenticationIdentity
from .providers import provider_fingerprint, providers


def cached_user_active(user, *, now=None):
    """Reload global suspension and require fresh external directory verification."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    from compute_broker.control.models import User
    current = User.objects.filter(id=user.pk, is_active=True).first()
    if current is None:
        return False
    try:
        configured = providers()
        identity = AuthenticationIdentity.objects.filter(user=current).first()
        if identity is None:
            return bool(current.local_login_allowed and configured["local"]["enabled"])
        config = configured.get(identity.provider_id)
        now = now or timezone.now()
        return bool(identity.enabled and identity.directory_active and identity.verified_at is not None
            and config and config["kind"] == "ldap" and config["enabled"]
            and identity.provider_config_sha256 == provider_fingerprint(config)
            and identity.verified_at <= now < identity.verified_at + timedelta(seconds=config["freshness_seconds"]))
    except (ImproperlyConfigured, ValueError, TypeError, KeyError):
        return False


def refresh_external_identity(user, *, adapter_factory=LDAPAdapter):
    """Explicit bounded service lookup. Invoke outside every DB transaction.

    Directory lookup never clears operator suspension on either User or link.
    Failures retain no diagnostics and cannot manufacture fresh entitlements.
    """
    if transaction.get_connection().in_atomic_block:
        raise RuntimeError("Directory refresh must run outside database transactions")
    from compute_broker.control.models import User
    user_id = getattr(user, "pk", user)
    current = User.objects.filter(pk=user_id, is_active=True).first()
    identity = AuthenticationIdentity.objects.filter(user_id=user_id, enabled=True).first()
    if current is None or identity is None:
        return False
    try:
        config = providers().get(identity.provider_id)
        if not config or config["kind"] != "ldap" or not config["enabled"]:
            return False
        principal = adapter_factory(config).lookup_subject(identity.subject)
        active = principal.subject == identity.subject and principal.active
    except DirectoryFailure as error:
        if error.code not in {"SUBJECT_NOT_FOUND", "DIRECTORY_SUBJECT_CHANGED", "DIRECTORY_RECORD_INVALID", "DIRECTORY_RECORD_AMBIGUOUS", "DIRECTORY_STATUS_UNKNOWN"}:
            return False
        active = False
        principal = None
    except (ImproperlyConfigured, ValueError, TypeError, KeyError):
        return False
    with transaction.atomic():
        current = User.objects.select_for_update().get(pk=user_id)
        updated = AuthenticationIdentity.objects.select_for_update().get(pk=identity.pk)
        if (not current.is_active or not updated.enabled or updated.provider_id != identity.provider_id
                or updated.subject != identity.subject):
            return False
        updated.directory_active = active
        updated.verified_at = timezone.now()
        updated.provider_config_sha256 = provider_fingerprint(config)
        if principal is not None and principal.subject == updated.subject:
            updated.login_name = principal.login_name
            updated.display_name = principal.display_name
        updated.save(update_fields=["directory_active", "verified_at", "provider_config_sha256", "login_name", "display_name"])
    return active


def refresh_due_identities(*, limit=20, adapter_factory=LDAPAdapter):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Refresh limit must be between one and 100")
    configured = providers()
    checked = active = 0
    now = timezone.now()
    for identifier, config in configured.items():
        if config["kind"] != "ldap" or not config["enabled"] or checked >= limit:
            continue
        # Proactively refresh after half the validity interval, with bounded work.
        from django.db.models import Q
        candidates = AuthenticationIdentity.objects.filter(provider_id=identifier, enabled=True, user__is_active=True).filter(
            Q(verified_at__isnull=True) | Q(verified_at__lte=now - timedelta(seconds=max(1, config["freshness_seconds"] // 2))))
        for identity in candidates.order_by("verified_at", "id")[:limit - checked]:
            checked += 1
            active += int(refresh_external_identity(identity.user_id, adapter_factory=adapter_factory))
    return {"checked": checked, "active": active, "not_refreshed_or_inactive": checked - active}
