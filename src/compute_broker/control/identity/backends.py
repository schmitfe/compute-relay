"""One explicitly routed backend: LDAP rejection cannot fall back to local."""

from uuid import uuid4

from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .authority import cached_user_active
from .ldap_adapter import DirectoryFailure, LDAPAdapter
from .models import AuthenticationIdentity
from .providers import provider_fingerprint, providers

BACKEND_PATH = "compute_broker.control.identity.backends.IdentityRoutingBackend"


def _bind_principal(identifier, config, principal):
    from compute_broker.control.models import User
    if not principal.active:
        return None
    for retry in range(2):
        try:
            with transaction.atomic():
                existing = AuthenticationIdentity.objects.filter(provider_id=identifier, subject=principal.subject).first()
                if existing is None:
                    user = User(username="ext_" + uuid4().hex, first_name=principal.first_name,
                        last_name=principal.last_name, email=principal.email, is_staff=False, is_superuser=False)
                    user.local_login_allowed = False
                    user.set_unusable_password()
                    user.save()
                    identity = AuthenticationIdentity(user=user, provider_id=identifier, subject=principal.subject)
                else:
                    # Same lock order as explicit refresh: User, then identity.
                    user = User.objects.select_for_update().get(pk=existing.user_id)
                    identity = AuthenticationIdentity.objects.select_for_update().get(pk=existing.pk)
                    if (not user.is_active or not identity.enabled or identity.user_id != user.id
                            or identity.provider_id != identifier or identity.subject != principal.subject):
                        return None
                    user.first_name, user.last_name, user.email = principal.first_name, principal.last_name, principal.email
                    user.local_login_allowed = False
                    if user.has_usable_password():
                        user.set_unusable_password()
                    user.save()
                identity.login_name = principal.login_name
                identity.display_name = principal.display_name
                identity.directory_active = True
                identity.verified_at = timezone.now()
                identity.provider_config_sha256 = provider_fingerprint(config)
                identity.save()
                return user
        except IntegrityError:
            # A concurrent first login may have created the same provider/subject.
            # Roll back the unlinked User too; never attach by name or email.
            if retry or not AuthenticationIdentity.objects.filter(provider_id=identifier, subject=principal.subject).exists():
                raise


class IdentityRoutingBackend(ModelBackend):
    @sensitive_variables()
    def authenticate(self, request, username=None, password=None, provider="local", **kwargs):
        try:
            if not isinstance(provider, str):
                raise PermissionDenied
            config = providers().get(provider)
            if (config is None or not config["enabled"] or not isinstance(username, str) or not 0 < len(username) <= 150
                    or not isinstance(password, str) or not 0 < len(password) <= 4096 or "\x00" in password):
                raise PermissionDenied
            username = username.strip()
            if provider == "local":
                from compute_broker.control.models import User
                candidate = User.objects.filter(username=username).first()
                if candidate is not None and (not candidate.local_login_allowed
                        or AuthenticationIdentity.objects.filter(user=candidate).exists()):
                    raise PermissionDenied
                user = super().authenticate(request, username=username, password=password, **kwargs)
                if user is None or not cached_user_active(user):
                    raise PermissionDenied
                return user
            if config["kind"] != "ldap" or transaction.get_connection().in_atomic_block:
                raise PermissionDenied
            principal = LDAPAdapter(config).authenticate(username, password)
            user = _bind_principal(provider, config, principal)
            if user is None:
                raise PermissionDenied
            return user
        except PermissionDenied:
            raise
        except Exception:
            # Stop Django's backend chain, including a mistakenly appended local
            # backend. Never log a raw LDAP exception or bind credentials.
            raise PermissionDenied from None

    def get_user(self, user_id):
        user = super().get_user(user_id)
        return user if user is not None and cached_user_active(user) else None
