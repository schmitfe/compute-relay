import hashlib
import secrets

from django.utils import timezone

from .errors import BrokerError
from .local_auth import current_user_active
from .models import ProjectCredential, User, WorkerIdentity


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
    # Identity discovery needs current ownership, not a job scope.
    require_credential_actor(credential, scope if scope is not None else "jobs:read")
    return credential


def require_credential_actor(credential, scope="jobs:execute"):
    user = User.objects.filter(id=credential.issued_by_id, is_active=True).first()
    if user is None:
        raise BrokerError("AUTH_REQUIRED", "The issuing user is disabled or this key has no verified owner.", 403)
    if not current_user_active(user):
        raise BrokerError("AUTH_REQUIRED", "The issuing user is not the active local owner.", 403)
    role = "viewer" if scope in {"jobs:read", "artifacts:read"} else "submitter"
    human_permission(user, credential.project, role)
    return user


def worker_identity(request):
    worker = WorkerIdentity.objects.filter(token_hash=token_hash(bearer(request)), revoked=False).first()
    if worker is None:
        raise BrokerError("AUTH_REQUIRED", "A valid worker credential is required.", 401)
    return worker


def allowed_connections(credential):
    """A key can narrow its project authority; it can never widen it."""
    query = credential.project.connections.filter(enabled=True, archived=False, setup_complete=True,
        owner_id=credential.issued_by_id, worker__owner_id=credential.issued_by_id, worker__revoked=False)
    if credential.issued_by_id is None or credential.project.owner_id != credential.issued_by_id:
        return query.none()
    if credential.allowed_connection_ids is not None:
        query = query.filter(id__in=credential.allowed_connection_ids)
    return query


def connection_allowed(credential, connection_id):
    return allowed_connections(credential).filter(id=connection_id).exists()


def require_connection(credential, connection_id):
    if not connection_allowed(credential, connection_id):
        raise BrokerError("CONNECTION_DENIED", "This key cannot access the cluster.", 403)


def human_permission(user, project, role):
    if not user.is_authenticated or not user.is_active:
        raise BrokerError("AUTH_REQUIRED", "Sign in with a human account.", 401)
    if not current_user_active(user):
        raise BrokerError("AUTH_REQUIRED", "The active local owner is required.", 403)
    if project.owner_id == user.id:
        return
    raise BrokerError("SCOPE_DENIED", "This project belongs to another account.", 403)
