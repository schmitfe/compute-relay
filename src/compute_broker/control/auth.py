import hashlib
import secrets

from django.utils import timezone

from .errors import BrokerError
from .models import Membership, ProjectCredential, User, WorkerIdentity


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(kind="cbp"):
    return kind + "_" + secrets.token_urlsafe(32)


def bearer(request):
    value = request.headers.get("Authorization", "")
    if not value.startswith("Bearer ") or len(value) > 256:
        raise BrokerError("AUTH_REQUIRED", "A valid bearer credential is required.", 401)
    return value[7:]


def project_credential(request, scope=None):
    credential = ProjectCredential.objects.select_related("project").filter(token_hash=token_hash(bearer(request)), revoked=False, expires_at__gt=timezone.now()).first()
    if credential is None:
        raise BrokerError("AUTH_REQUIRED", "Credential is invalid, revoked or expired.", 401)
    if scope is not None and scope not in credential.scopes:
        raise BrokerError("SCOPE_DENIED", "Credential does not permit this operation.", 403)
    # Identity discovery needs current project membership, not a job scope.
    require_credential_actor(credential, scope if scope is not None else "jobs:read")
    return credential


def require_credential_actor(credential, scope="jobs:execute"):
    user = User.objects.filter(id=credential.issued_by_id, is_active=True).first()
    if user is None:
        raise BrokerError("AUTH_REQUIRED", "The issuing user is disabled or this key has no verified owner.", 403)
    from .identity.authority import cached_user_active
    if not cached_user_active(user):
        raise BrokerError("AUTH_REQUIRED", "The issuing user's account verification has expired or access was withdrawn.", 403)
    role = "viewer" if scope in {"jobs:read", "artifacts:read"} else "submitter"
    human_permission(user, credential.project, role)
    return user


def worker_identity(request):
    worker = WorkerIdentity.objects.filter(token_hash=token_hash(bearer(request)), revoked=False).first()
    if worker is None:
        raise BrokerError("AUTH_REQUIRED", "A valid worker credential is required.", 401)
    return worker


def connection_allowed(credential, connection_id):
    """A key can narrow its project authority; it can never widen it."""
    restricted = credential.allowed_connection_ids
    return credential.issued_by_id is not None and credential.project.connections.filter(
        id=connection_id, enabled=True, archived=False, setup_complete=True, owner_id=credential.issued_by_id,
        worker__owner_id=credential.issued_by_id, worker__revoked=False).exists() and (
        restricted is None or str(connection_id) in restricted)


def require_connection(credential, connection_id):
    if not connection_allowed(credential, connection_id):
        raise BrokerError("CONNECTION_DENIED", "This key cannot access the cluster.", 403)


def human_permission(user, project, role):
    if not user.is_authenticated or not user.is_active:
        raise BrokerError("AUTH_REQUIRED", "Sign in with a human account.", 401)
    from .identity.authority import cached_user_active
    if not cached_user_active(user):
        raise BrokerError("AUTH_REQUIRED", "Account verification is required.", 403)
    if project.owner_id == user.id or user.is_superuser:
        return
    membership = Membership.objects.filter(project=project, user=user).first()
    implied_viewer = role == "viewer" and membership and bool(set(membership.roles) & {"submitter", "approver"})
    if membership and (role in membership.roles or "project_admin" in membership.roles or implied_viewer):
        return
    raise BrokerError("SCOPE_DENIED", "Project role does not permit this action.", 403)
