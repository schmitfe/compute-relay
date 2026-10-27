"""One local owner, ordinary passwords, and explicitly password-free sessions."""

from uuid import uuid4

from django.contrib.auth import authenticate
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
import pytest

from compute_relay.control.auth import human_permission
from compute_relay.control.errors import BrokerError
from compute_relay.control.local_auth import LocalOwnerBackend, current_user_active
from compute_relay.control.local_auth_config import local_auth_configuration
from compute_relay.control.models import Project, User


@pytest.fixture
def owner(db, settings):
    owner = User.objects.create_user(username="local", password="synthetic-password")
    settings.BROKER_LOCAL_OWNER_ID = owner.id
    return owner


@pytest.fixture
def password_free(owner, settings, tmp_path):
    settings.BROKER_LOCAL_AUTH_MODE = "none"
    settings.BROKER_LOCAL_RUNTIME = "1"
    settings.BROKER_LOCAL_ORIGIN = "http://127.0.0.1:8765"
    settings.ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
    settings.DATA_DIR = tmp_path / "state"
    settings.BROKER_INSTANCE_ID = uuid4()
    from compute_relay.local_state import LocalState
    from compute_relay.portal_tokens import issue
    with LocalState(settings.DATA_DIR) as state:
        owner.launch_path = issue(state, settings.BROKER_LOCAL_ORIGIN, settings.BROKER_INSTANCE_ID).removeprefix(settings.BROKER_LOCAL_ORIGIN)
    owner.set_unusable_password()
    owner.save(update_fields=["password"])
    return owner


def test_password_backend_pins_both_login_and_existing_sessions_to_owner(owner):
    other = User.objects.create_superuser(username="foreign", password="synthetic-password")
    assert authenticate(username="local", password="synthetic-password").id == owner.id
    assert authenticate(username="foreign", password="synthetic-password") is None
    assert LocalOwnerBackend().get_user(other.id) is None
    assert not current_user_active(other)
    User.objects.filter(id=owner.id).update(is_active=False)
    assert not current_user_active(owner)
    assert LocalOwnerBackend().get_user(owner.id) is None


def test_owner_cannot_read_foreign_project_even_if_superuser(owner):
    other = User.objects.create_user(username="foreign")
    project = Project.objects.create(owner=other, name="Foreign project")
    owner.is_superuser = True
    owner.save(update_fields=["is_superuser"])
    with pytest.raises(BrokerError) as denied:
        human_permission(owner, project, "viewer")
    assert denied.value.code == "SCOPE_DENIED"


def test_login_has_no_provider_or_account_provisioning(owner):
    client = Client()
    page = client.get("/login/")
    assert b'name="provider"' not in page.content
    before = User.objects.count()
    assert client.post("/login/", {"username": "new-user", "password": "synthetic"}).status_code == 200
    assert User.objects.count() == before
    assert client.post("/login/", {"username": "local", "password": "synthetic-password"}).status_code == 302
    assert client.session["_auth_user_id"] == str(owner.id)


def test_password_free_token_establishes_ordinary_owner_session(password_free):
    client = Client()
    response = client.get(password_free.launch_path, HTTP_HOST="127.0.0.1:8765", REMOTE_ADDR="127.0.0.1", HTTP_ACCEPT="text/html")
    assert response.status_code == 302 and response.url == "/"
    assert response["Referrer-Policy"] == "no-referrer" and "no-store" in response["Cache-Control"]
    assert client.session["_auth_user_id"] == str(password_free.id)
    login_page = client.get("/login/", HTTP_HOST="127.0.0.1:8765")
    assert login_page.status_code == 302 and login_page.url == "/"


@pytest.mark.parametrize("path,headers", [
    ("/api/v1/context", {}), ("/internal/v1/lease", {}), ("/ui/live/dashboard/", {}),
    ("/", {"HTTP_AUTHORIZATION": "Bearer synthetic-project-key"}),
    ("/", {"HTTP_ACCEPT": "application/json"}),
    ("/", {"REMOTE_ADDR": "192.0.2.1"}),
    ("/", {"HTTP_HOST": "localhost:8765"}),
    ("/", {"HTTP_HOST": "127.0.0.1:9999"}),
])
def test_password_free_never_bootstraps_api_bearer_remote_or_wrong_origin(password_free, path, headers):
    client = Client()
    options = {"HTTP_HOST": "127.0.0.1:8765", "REMOTE_ADDR": "127.0.0.1", "HTTP_ACCEPT": "text/html", **headers}
    client.get(password_free.launch_path if path == "/" else path, **options)
    assert "_auth_user_id" not in client.session


def test_password_free_does_not_bootstrap_post_or_bypass_csrf(password_free):
    client = Client(enforce_csrf_checks=True)
    headers = {"HTTP_HOST": "127.0.0.1:8765", "REMOTE_ADDR": "127.0.0.1"}
    assert client.post("/projects/new/", {}, **headers).status_code == 403
    assert "_auth_user_id" not in client.session
    assert client.get("/projects/new/", **headers).status_code == 302
    assert client.get(password_free.launch_path, **headers).status_code == 302
    assert client.session["_auth_user_id"] == str(password_free.id)
    assert client.post("/projects/new/", {}, **headers).status_code == 403


def test_password_free_does_not_enter_disabled_owner(password_free):
    User.objects.filter(id=password_free.id).update(is_active=False)
    client = Client()
    assert client.get("/", HTTP_HOST="127.0.0.1:8765").status_code == 302
    assert "_auth_user_id" not in client.session


def test_launch_token_is_single_use_and_head_does_not_consume(password_free):
    headers = {"HTTP_HOST": "127.0.0.1:8765"}
    first, second = Client(), Client()
    first.head(password_free.launch_path, **headers)
    assert "_auth_user_id" not in first.session
    first.get(password_free.launch_path, **headers)
    second.get(password_free.launch_path, **headers)
    assert first.session["_auth_user_id"] == str(password_free.id)
    assert "_auth_user_id" not in second.session


@pytest.mark.parametrize("invalid", ["expired", "other_instance", "wrong_token"])
def test_invalid_launch_token_does_not_log_in(password_free, settings, invalid):
    from compute_relay.local_state import LocalState
    with LocalState(settings.DATA_DIR) as state:
        record = state.read("portal-token.json")
        if invalid == "expired":
            record["expires_at"] = 0
        elif invalid == "other_instance":
            record["instance_id"] = str(uuid4())
        else:
            record["sha256"] = "0" * 64
        state.write("portal-token.json", record)
    client = Client()
    client.get(password_free.launch_path, HTTP_HOST="127.0.0.1:8765")
    assert "_auth_user_id" not in client.session


def test_health_identifies_only_the_current_local_instance(owner, settings):
    installation, instance = uuid4(), uuid4()
    settings.BROKER_LOCAL_RUNTIME = "1"
    settings.BROKER_INSTALLATION_ID = installation
    settings.BROKER_INSTANCE_ID = instance
    settings.PROFILE = "local"
    client = Client()
    result = client.get("/healthz").json()
    assert result["installation_id"] == str(installation) and result["instance_id"] == str(instance)
    assert result["profile"] == "local"
    settings.BROKER_LOCAL_RUNTIME = ""
    result = client.get("/healthz").json()
    assert "installation_id" not in result and "instance_id" not in result


@pytest.mark.parametrize("changes", [
    {"BROKER_LOCAL_AUTH_MODE": "other"}, {"BROKER_LOCAL_RUNTIME": ""},
    {"BROKER_LOCAL_OWNER_ID": ""}, {"BROKER_LOCAL_OWNER_ID": "invalid"},
    {"BROKER_LOCAL_ORIGIN": "http://0.0.0.0:8765"},
    {"BROKER_LOCAL_ORIGIN": "http://example.com:8765"},
    {"BROKER_LOCAL_ORIGIN": "http://127.0.0.1"},
    {"BROKER_LOCAL_ORIGIN": "http://127.0.0.1:0"},
    {"BROKER_LOCAL_ORIGIN": "http://user@127.0.0.1:8765"},
    {"BROKER_LOCAL_ORIGIN": "http://127.0.0.1:8765/other"},
    {"BROKER_LOCAL_ORIGIN": "http://127.0.0.1:8765?x=1"},
    {"BROKER_LOCAL_ORIGIN": "https://127.0.0.1:8765"},
])
def test_password_free_configuration_rejects_unfenced_origins(changes):
    environment = {"BROKER_LOCAL_AUTH_MODE": "none", "BROKER_LOCAL_RUNTIME": "1",
        "BROKER_LOCAL_OWNER_ID": str(uuid4()), "BROKER_LOCAL_ORIGIN": "http://127.0.0.1:8765", **changes}
    with pytest.raises(ImproperlyConfigured):
        local_auth_configuration(environment)
