"""Provider routing, transient LDAP credentials and stable subject boundaries."""

import copy
from datetime import timedelta
import json
import ssl
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.contrib.auth import authenticate
from django.contrib.sessions.models import Session
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import transaction
from django.test import Client
from django.utils import timezone

from compute_broker.control.identity import authority, backends, ldap_adapter, providers
from compute_broker.control.identity.ldap_adapter import DirectoryFailure, DirectoryPrincipal, LDAPAdapter
from compute_broker.control.identity.models import AuthenticationIdentity
from compute_broker.control.models import User


def configuration(tmp_path, **changes):
    password = tmp_path / "bind-password"
    password.write_text("synthetic-reader-password\n");password.chmod(0o600)
    return providers.validate_provider_configuration({"local": copy.deepcopy(providers.DEFAULT_PROVIDERS["local"]),
        "directory": {"kind": "ldap", "enabled": True, "label": "Fixture directory", "server_uri": "ldaps://directory.fixture.invalid:636",
            "start_tls": False, "ca_cert_file": str(tmp_path / "ca.pem"), "bind_dn": "cn=reader,dc=fixture,dc=invalid",
            "bind_password_file": str(password), "user_base_dn": "ou=people,dc=fixture,dc=invalid", **changes}})


@pytest.mark.parametrize("change", ["cleartext", "empty_bind", "inline_secret", "unbounded", "mutable_subject", "secret_attribute", "credentials_uri", "multiple_servers"])
def test_provider_configuration_rejects_unsafe_or_ambiguous_fields(tmp_path, change):
    document = configuration(tmp_path)
    target = document["directory"]
    if change == "cleartext": target["server_uri"] = "ldap://directory.fixture.invalid"
    elif change == "empty_bind": target["bind_dn"] = ""
    elif change == "inline_secret": target["bind_password"] = "must-not-be-stored"
    elif change == "unbounded": target["operation_timeout_seconds"] = 0
    elif change == "mutable_subject": target["subject_attribute"] = "uid"
    elif change == "secret_attribute": target["profile_attributes"] = {"email": "userPassword"}
    elif change == "credentials_uri": target["server_uri"] = "ldaps://reader:secret@directory.fixture.invalid"
    else: target["server_uri"] += " ldaps://other.fixture.invalid"
    with pytest.raises(ValueError):
        providers.validate_provider_configuration(document)


def test_config_loader_is_explicit_bounded_and_network_free(tmp_path, monkeypatch):
    document = configuration(tmp_path)
    path = tmp_path / "providers.json";path.write_text(json.dumps(document));path.chmod(0o600)
    monkeypatch.setattr(ldap_adapter, "_ldap", lambda: pytest.fail("configuration contacted LDAP"))
    assert providers.load_provider_configuration(path) == document
    assert providers.load_provider_configuration() == providers.DEFAULT_PROVIDERS
    path.chmod(0o666)
    with pytest.raises(ImproperlyConfigured): providers.load_provider_configuration(path)
    path.chmod(0o600);path.write_text('{"local":{},"local":{}}')
    with pytest.raises(ImproperlyConfigured): providers.load_provider_configuration(path)


@pytest.mark.parametrize("attribute", ["member", "uniqueMember"])
def test_dn_membership_attributes_are_not_mistaken_for_posix_login_names(tmp_path, attribute):
    with pytest.raises(ValueError, match="memberUid"):
        configuration(tmp_path, status_policy={"required_group_dn": "cn=allowed,dc=fixture,dc=invalid",
            "group_membership_attribute": attribute})


def record(subject=None, **attributes):
    return {"type": "searchResEntry", "dn": "uid=alice,ou=people,dc=fixture,dc=invalid", "raw_attributes": {
        "UID": [b"alice"], "entryUUID": [(subject or str(uuid4())).encode()], "mail": [b"same@fixture.invalid"],
        "givenName": [b"Alice"], "sn": [b"Fixture"], **attributes}}


def fake_library(monkeypatch, responses, *, failure=None):
    calls = []
    connections = []
    class Connection:
        def __init__(self, server, **options):
            self.user, self.password = options["user"], options["password"]
            self.closed = True;self.result = {};self.response = [];self.request = None
            calls.append(("connection", options));connections.append(self)
        def open(self, **kwargs):
            calls.append(("open", kwargs))
            if failure == "certificate": raise RuntimeError("raw-error-with-secret")
            self.closed = False
        def start_tls(self, **kwargs):
            calls.append(("tls", kwargs))
            return failure != "tls"
        def bind(self, **kwargs): calls.append(("bind", kwargs));return True
        def rebind(self, **kwargs):
            calls.append(("rebind", {key: value for key, value in kwargs.items() if key != "password"}))
            self.user, self.password = kwargs["user"], kwargs["password"]
            return failure != "credentials"
        def search(self, base, query, **kwargs):
            calls.append(("search", {"base": base, "query": query, **kwargs}))
            self.response = copy.deepcopy(responses.pop(0));self.result = {"result": 0}
            return bool(self.response)
        def unbind(self): calls.append(("unbind", {}));self.closed = True
    library = SimpleNamespace(Tls=lambda **kw: calls.append(("tls_config", kw)) or kw,
        Server=lambda host, **kw: calls.append(("server", {"host": host, **kw})) or kw,
        Connection=Connection, NONE="NONE", SIMPLE="SIMPLE", SYNC="SYNC", AUTO_BIND_NONE="NONE", SUBTREE="SUBTREE", BASE="BASE", DEREF_NEVER="NEVER")
    monkeypatch.setattr(ldap_adapter, "_ldap", lambda: library)
    return calls, connections


@pytest.mark.parametrize("start_tls", [False, True])
def test_adapter_uses_verified_tls_before_bind_and_clears_transient_credentials(tmp_path, monkeypatch, start_tls):
    config = configuration(tmp_path, server_uri="ldap://directory.fixture.invalid:389" if start_tls else "ldaps://directory.fixture.invalid:636", start_tls=start_tls)["directory"]
    row = record();calls, connections = fake_library(monkeypatch, [[row], [row]])
    result = LDAPAdapter(config).authenticate("alice)(uid=*)", "synthetic-user-password")
    assert result.subject == row["raw_attributes"]["entryUUID"][0].decode() and result.login_name == "alice"
    tls = next(value for name, value in calls if name == "tls_config")
    assert tls["validate"] == ssl.CERT_REQUIRED and tls["valid_names"] == ["directory.fixture.invalid"]
    options = next(value for name, value in calls if name == "connection")
    assert options["auto_bind"] == "NONE" and options["auto_referrals"] is False and options["read_only"] is True
    assert options["receive_timeout"] == 5 and options["check_names"] is False
    search = next(value for name, value in calls if name == "search")
    assert "alice\\29\\28uid=\\2a\\29" in search["query"]
    assert search["size_limit"] == 2 and search["time_limit"] == 5 and "userPassword" not in search["attributes"]
    names = [name for name, _ in calls]
    if start_tls: assert names.index("tls") < names.index("bind")
    assert connections[0].password is connections[0].user is connections[0].request is None


@pytest.mark.parametrize("failure", ["certificate", "tls", "credentials"])
def test_ldap_failure_never_downgrades_tls_or_discloses_library_details(tmp_path, monkeypatch, failure):
    config = configuration(tmp_path, server_uri="ldap://directory.fixture.invalid", start_tls=True)["directory"]
    row = record();calls, _ = fake_library(monkeypatch, [[row], [row]], failure=failure)
    with pytest.raises(DirectoryFailure) as error:
        LDAPAdapter(config).authenticate("alice", "synthetic-user-password")
    assert "raw-error" not in str(error.value) and "synthetic-user-password" not in str(error.value)
    if failure in {"certificate", "tls"}: assert not any(name == "bind" for name, _ in calls)


@pytest.mark.parametrize("password", ["", None, "\x00", "x" * 4097])
def test_empty_or_unbounded_password_is_rejected_before_contact(tmp_path, monkeypatch, password):
    monkeypatch.setattr(ldap_adapter, "_ldap", lambda: pytest.fail("empty-password LDAP operation"))
    with pytest.raises(DirectoryFailure): LDAPAdapter(configuration(tmp_path)["directory"]).authenticate("alice", password)


@pytest.mark.parametrize("case", ["referral", "duplicate", "missing_subject", "changed_subject"])
def test_referrals_ambiguous_records_and_dn_recreation_fail_closed(tmp_path, monkeypatch, case):
    row = record();first = [row];second = [row]
    if case == "referral": first = [{"type": "searchResRef", "uri": ["ldap://attacker.invalid"]}]
    elif case == "duplicate": first = [row, row]
    elif case == "missing_subject": row["raw_attributes"].pop("entryUUID")
    else: second = [record()]
    fake_library(monkeypatch, [first, second])
    with pytest.raises(DirectoryFailure): LDAPAdapter(configuration(tmp_path)["directory"]).authenticate("alice", "password")


@pytest.mark.parametrize("attributes,active", [({"sambaAcctFlags": [b"[U          ]"], "shadowExpire": [b"99999"]}, True),
    ({"sambaAcctFlags": [b"[UD         ]"], "shadowExpire": [b"99999"]}, False),
    ({"sambaAcctFlags": [b"[UL         ]"], "shadowExpire": [b"99999"]}, False),
    ({"sambaAcctFlags": [b"[U          ]"], "shadowExpire": [b"1"]}, False)])
def test_explicit_shadow_and_samba_status_policy(tmp_path, monkeypatch, attributes, active):
    row = record(**attributes);fake_library(monkeypatch, [[row], [row]])
    config = configuration(tmp_path, status_policy={"samba_account_flags": True, "shadow_expire": True})["directory"]
    assert LDAPAdapter(config).authenticate("alice", "password").active is active


def test_configured_but_unknown_status_and_missing_group_deny(tmp_path, monkeypatch):
    row = record(shadowExpire=[b"0"]);fake_library(monkeypatch, [[row], [row]])
    with pytest.raises(DirectoryFailure):
        LDAPAdapter(configuration(tmp_path, status_policy={"shadow_expire": True})["directory"]).authenticate("alice", "password")
    row = record();fake_library(monkeypatch, [[row], [row], []])
    assert LDAPAdapter(configuration(tmp_path, status_policy={"required_group_dn": "cn=allowed,dc=fixture,dc=invalid"})["directory"]).authenticate("alice", "password").active is False


@pytest.fixture
def identities(transactional_db, settings, tmp_path, monkeypatch):
    settings.BROKER_IDENTITY_PROVIDERS = configuration(tmp_path, status_policy={"shadow_expire": True})
    settings.AUTHENTICATION_BACKENDS = [backends.BACKEND_PATH]
    state = SimpleNamespace(subject=str(uuid4()), name="alice", active=True, fail=False)
    class Adapter:
        def __init__(self, config): pass
        def authenticate(self, username, password):
            if state.fail or password != "ldap-secret": raise DirectoryFailure()
            return DirectoryPrincipal(state.subject, state.name, active=state.active, email="same@fixture.invalid")
        def lookup_subject(self, subject):
            if state.fail: raise DirectoryFailure()
            return DirectoryPrincipal(state.subject, state.name, active=state.active)
    monkeypatch.setattr(backends, "LDAPAdapter", Adapter)
    state.adapter = Adapter
    return state


def test_local_and_directory_same_username_email_never_merge_or_fall_back(identities):
    local = User.objects.create_user(username="alice", password="local-secret", email="same@fixture.invalid")
    assert authenticate(username="alice", password="local-secret").id == local.id
    assert authenticate(provider="directory", username="alice", password="local-secret") is None
    external = authenticate(provider="directory", username="alice", password="ldap-secret")
    assert external.id != local.id and external.username != local.username
    assert external.local_login_allowed is False and not external.has_usable_password()
    assert not external.is_superuser and not external.is_staff
    assert AuthenticationIdentity.objects.get(user=external).subject == identities.subject
    external.set_password("local-secret");external.save()
    assert authenticate(provider="local", username=external.username, password="local-secret") is None


def test_directory_rename_retains_subject_and_recreation_has_no_old_ownership(identities):
    first = authenticate(provider="directory", username="alice", password="ldap-secret")
    identities.name = "renamed"
    assert authenticate(provider="directory", username="renamed", password="ldap-secret").id == first.id
    assert AuthenticationIdentity.objects.get(user=first).login_name == "renamed"
    identities.subject = str(uuid4())
    second = authenticate(provider="directory", username="renamed", password="ldap-secret")
    assert second.id != first.id and AuthenticationIdentity.objects.count() == 2


def test_directory_suspension_and_stale_cache_block_sessions_and_never_autoenable(identities):
    user = authenticate(provider="directory", username="alice", password="ldap-secret")
    identity = AuthenticationIdentity.objects.get(user=user)
    assert authority.cached_user_active(user)
    identity.verified_at = timezone.now() - timedelta(seconds=301);identity.save()
    assert backends.IdentityRoutingBackend().get_user(user.id) is None
    assert authority.refresh_external_identity(user, adapter_factory=identities.adapter)
    assert authority.cached_user_active(user)
    identities.active = False
    assert not authority.refresh_external_identity(user, adapter_factory=identities.adapter)
    assert not authority.cached_user_active(user)
    identities.active = True
    User.objects.filter(id=user.id).update(is_active=False)
    assert not authority.refresh_external_identity(user, adapter_factory=identities.adapter)
    assert authenticate(provider="directory", username="alice", password="ldap-secret") is None
    user.refresh_from_db();assert user.is_active is False


def test_outage_does_not_renew_freshness_and_operator_link_disable_survives_login(identities):
    user = authenticate(provider="directory", username="alice", password="ldap-secret")
    identity = AuthenticationIdentity.objects.get(user=user);verified = identity.verified_at
    identities.fail = True
    assert not authority.refresh_external_identity(user, adapter_factory=identities.adapter)
    identity.refresh_from_db();assert identity.verified_at == verified
    identities.fail = False
    identity.enabled = False;identity.save()
    assert authenticate(provider="directory", username="alice", password="ldap-secret") is None
    assert not authority.cached_user_active(user)
    with transaction.atomic(), pytest.raises(RuntimeError):
        authority.refresh_external_identity(user, adapter_factory=identities.adapter)


def test_provider_failure_stops_appended_local_backend_and_ui_needs_explicit_route(identities, settings):
    User.objects.create_user(username="alice", password="local-secret")
    settings.AUTHENTICATION_BACKENDS = [backends.BACKEND_PATH, "django.contrib.auth.backends.ModelBackend"]
    assert authenticate(provider="directory", username="alice", password="local-secret") is None
    client = Client(enforce_csrf_checks=True);page = client.get("/login/")
    assert b'<select name="provider">' in page.content
    response = client.post("/login/", {"provider": "directory", "username": "alice", "password": "local-secret"},
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
    assert response.status_code == 200 and "_auth_user_id" not in client.session


def test_successful_login_persists_no_ldap_password_and_get_hides_unneeded_select(identities, settings, caplog):
    client = Client(enforce_csrf_checks=True);client.get("/login/")
    response = client.post("/login/", {"provider": "directory", "username": "alice", "password": "ldap-secret"},
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
    assert response.status_code == 302 and "_auth_user_id" in client.session
    persisted = {"users": list(User.objects.values()), "identities": list(AuthenticationIdentity.objects.values()),
        "sessions": [item.get_decoded() for item in Session.objects.all()]}
    assert "ldap-secret" not in json.dumps(persisted, default=str) and "ldap-secret" not in caplog.text
    settings.BROKER_IDENTITY_PROVIDERS = providers.DEFAULT_PROVIDERS
    assert b'<select name="provider">' not in Client().get("/login/").content
