"""Password authentication and current ownership for a personal installation."""

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.views.decorators.debug import sensitive_variables

from .local_auth_config import configured_owner


BACKEND_PATH = "compute_relay.control.local_auth.LocalOwnerBackend"


def current_user_active(user):
    """Reload account state so existing sessions and keys cannot bypass removal."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    owner_id = configured_owner(getattr(settings, "BROKER_LOCAL_OWNER_ID", None))
    if owner_id is not None and user.pk != owner_id:
        return False
    from .models import User
    return User.objects.filter(id=user.pk, is_active=True).exists()


class LocalOwnerBackend(ModelBackend):
    """Use Django's password checks, restricting sessions to the local owner."""

    @sensitive_variables("password")
    def authenticate(self, request, username=None, password=None, **kwargs):
        if (not isinstance(username, str) or not 0 < len(username) <= 150
                or not isinstance(password, str) or not 0 < len(password) <= 4096
                or "\x00" in password):
            return None
        user = super().authenticate(request, username=username, password=password, **kwargs)
        return user if current_user_active(user) else None

    def get_user(self, user_id):
        user = super().get_user(user_id)
        return user if current_user_active(user) else None


class LocalOwnerSessionMiddleware:
    """Exchange a private launch token for an ordinary owner session."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (getattr(settings, "BROKER_LOCAL_AUTH_MODE", "password") == "none"
                and request.method == "GET" and request.path == "/login/"
                and "launch_token" in request.GET
                and "Authorization" not in request.headers and request.accepts("text/html")):
            import ipaddress
            from urllib.parse import urlsplit
            from django.contrib.auth import login
            from .local_auth_config import local_auth_configuration
            # Recheck all fences even if settings were overridden after startup.
            owner_id, _, origin = local_auth_configuration({
                "BROKER_LOCAL_AUTH_MODE": "none", "BROKER_LOCAL_OWNER_ID": settings.BROKER_LOCAL_OWNER_ID,
                "BROKER_LOCAL_RUNTIME": getattr(settings, "BROKER_LOCAL_RUNTIME", ""),
                "BROKER_LOCAL_ORIGIN": getattr(settings, "BROKER_LOCAL_ORIGIN", "")})
            try:
                loopback = ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")).is_loopback
            except ValueError:
                loopback = False
            if loopback and request.get_host() == urlsplit(origin).netloc:
                from compute_relay.portal_tokens import consume
                from django.http import HttpResponseRedirect
                from .models import User
                user = User.objects.filter(id=owner_id, is_active=True).first()
                if user is not None and consume(settings.DATA_DIR, request.GET["launch_token"],
                        getattr(settings, "BROKER_INSTANCE_ID", None)):
                    login(request, user, backend=BACKEND_PATH)
                    response = HttpResponseRedirect("/")
                    response["Cache-Control"] = "no-store, private"
                    response["Referrer-Policy"] = "no-referrer"
                    return response
        return self.get_response(request)
