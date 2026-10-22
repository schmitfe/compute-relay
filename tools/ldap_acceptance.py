#!/usr/bin/env python3
"""Opt-in native LDAP/TLS acceptance against the synthetic internal Docker fixture."""

import argparse
import json
import os
from pathlib import Path
import tempfile


def run(ca_file, wrong_ca_file, password_file, user_password_directory):
    run.stage = "fixture_credentials"
    passwords = {name: (user_password_directory / (name + "-password")).read_text().rstrip("\n")
        for name in ("alice", "bob", "disabled", "expired")}
    # A fresh SQLite database is mandatory; never inherit a configured service DB.
    with tempfile.TemporaryDirectory(prefix="broker-ldap-acceptance-") as directory:
        root = Path(directory)
        os.environ.update(DJANGO_SETTINGS_MODULE="compute_broker.control.settings", BROKER_EXTENSIONS="",
            BROKER_PROFILE="development", BROKER_DB_ENGINE="sqlite", BROKER_DB_PATH=str(root / "test.sqlite3"),
            BROKER_DATA_DIR=str(root), BROKER_ARTIFACT_ROOT=str(root / "objects"), BROKER_SECRET_KEY_FILE=str(root / "django-secret"))
        os.environ.pop("BROKER_IDENTITY_CONFIG_FILE", None)
        import django
        django.setup()
        from django.conf import settings
        from django.contrib.auth import authenticate
        from django.core.management import call_command
        from django.utils import timezone
        from datetime import timedelta
        from compute_broker.control.identity.authority import cached_user_active, refresh_external_identity
        from compute_broker.control.identity.backends import BACKEND_PATH
        from compute_broker.control.identity.ldap_adapter import DirectoryFailure, LDAPAdapter
        from compute_broker.control.identity.models import AuthenticationIdentity
        from compute_broker.control.identity.providers import validate_provider_configuration
        from compute_broker.control.models import User
        run.stage = "isolated_database_migration"
        call_command("migrate", verbosity=0, interactive=False)
        configured = validate_provider_configuration({"local": {"kind": "local", "enabled": True, "label": "Local"},
            "fixture": {"kind": "ldap", "enabled": True, "label": "Synthetic LDAP", "server_uri": "ldaps://ldap-fixture:1636",
                "start_tls": False, "ca_cert_file": str(ca_file), "bind_dn": "cn=broker-reader,dc=fixture,dc=invalid",
                "bind_password_file": str(password_file), "user_base_dn": "ou=people,dc=fixture,dc=invalid",
                "status_policy": {"shadow_expire": True, "samba_account_flags": True,
                    "required_group_dn": "cn=allowed,ou=groups,dc=fixture,dc=invalid"}}})
        settings.BROKER_IDENTITY_PROVIDERS = configured
        settings.AUTHENTICATION_BACKENDS = [BACKEND_PATH]
        local = User.objects.create_user(username="alice", password="local-fixture-password", email="same@fixture.invalid")
        run.stage = "native_ldaps_eligible_user"
        user = authenticate(provider="fixture", username="alice", password=passwords["alice"])
        assert user is not None and user.id != local.id and not user.local_login_allowed and not user.has_usable_password()
        identity = AuthenticationIdentity.objects.get(user=user)
        assert identity.subject and identity.login_name == "alice" and cached_user_active(user)
        run.stage = "provider_collision_and_eligibility"
        assert authenticate(provider="fixture", username="alice", password="local-fixture-password") is None
        assert authenticate(provider="local", username="alice", password="local-fixture-password").id == local.id
        assert authenticate(provider="fixture", username="bob", password=passwords["bob"]) is not None
        assert authenticate(provider="fixture", username="disabled", password=passwords["disabled"]) is None
        assert authenticate(provider="fixture", username="expired", password=passwords["expired"]) is None
        config = configured["fixture"]
        for changed in ({"ca_cert_file": str(wrong_ca_file)}, {"server_uri": "ldaps://ldap-wrong-name:1636"}):
            run.stage = "untrusted_tls_rejection"
            invalid = {**config, **changed}
            try:
                LDAPAdapter(invalid).authenticate("alice", passwords["alice"])
            except DirectoryFailure:
                pass
            else:
                raise AssertionError("Untrusted certificate or hostname was accepted")
        run.stage = "native_starttls"
        start_tls = {**config, "server_uri": "ldap://ldap-fixture:1389", "start_tls": True}
        assert LDAPAdapter(start_tls).authenticate("alice", passwords["alice"]).subject == identity.subject
        run.stage = "freshness_and_operator_suspension"
        identity.verified_at = timezone.now() - timedelta(seconds=301);identity.save()
        assert not cached_user_active(user)
        assert refresh_external_identity(user) and cached_user_active(user)
        User.objects.filter(id=user.id).update(is_active=False)
        assert not cached_user_active(user) and not refresh_external_identity(user)
        assert authenticate(provider="fixture", username="alice", password=passwords["alice"]) is None
        run.stage = "password_persistence"
        persisted = json.dumps({"users": list(User.objects.values()), "identities": list(AuthenticationIdentity.objects.values())}, default=str)
        assert not any(value in persisted for value in passwords.values())
        assert password_file.read_text().rstrip("\n") not in persisted
        print(json.dumps({"ok": True, "fixture_only": True, "ldaps": True, "start_tls": True,
            "wrong_ca_rejected": True, "wrong_hostname_rejected": True, "expired_account_rejected": True,
            "disabled_account_rejected": True, "second_eligible_user": True,
            "local_collision_separated": True, "stale_refresh_checked": True, "global_suspension_preserved": True,
            "plaintext_passwords_persisted": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-disposable-fixture", action="store_true")
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--wrong-ca-file", type=Path, required=True)
    parser.add_argument("--bind-password-file", type=Path, required=True)
    parser.add_argument("--user-password-directory", type=Path, required=True)
    args = parser.parse_args()
    if not args.confirm_disposable_fixture:
        parser.error("Supply --confirm-disposable-fixture; this contacts only the fixed synthetic Docker directory")
    try:
        run(args.ca_file, args.wrong_ca_file, args.bind_password_file, args.user_password_directory)
    except Exception as error:
        # No LDAP response, username, DN, secret, or exception message is printed.
        print(json.dumps({"ok": False, "error_type": type(error).__name__, "stage": getattr(run, "stage", "setup")}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
