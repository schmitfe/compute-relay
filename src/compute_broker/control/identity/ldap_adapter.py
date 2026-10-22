"""Bounded LDAP operations through ldap3, with no LDAP wire implementation.

Reference: https://ldap3.readthedocs.io/en/latest/ssltls.html
and https://ldap3.readthedocs.io/en/latest/connection.html
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import re
import ssl
import stat
from urllib.parse import urlsplit
from uuid import UUID

from django.views.decorators.debug import sensitive_variables


class DirectoryFailure(Exception):
    """Only fixed codes escape the adapter; never LDAP responses or credentials."""

    def __init__(self, code="DIRECTORY_UNAVAILABLE"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DirectoryPrincipal:
    subject: str
    login_name: str
    active: bool = True
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    display_name: str = ""

    def __post_init__(self):
        if (not isinstance(self.subject, str) or _subject(self.subject) != self.subject
                or not isinstance(self.login_name, str) or not 0 < len(self.login_name) <= 150
                or any(ord(c) < 32 or ord(c) == 127 for c in self.login_name) or type(self.active) is not bool):
            raise DirectoryFailure("DIRECTORY_RECORD_INVALID")
        for field, maximum in (("first_name", 150), ("last_name", 150), ("email", 254), ("display_name", 255)):
            value = getattr(self, field)
            if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise DirectoryFailure("DIRECTORY_RECORD_INVALID")


def _ldap():
    try:
        import ldap3
        from ldap3.utils.log import OFF, set_library_log_detail_level, set_library_log_hide_sensitive_data
        set_library_log_hide_sensitive_data(True)
        set_library_log_detail_level(OFF)
        return ldap3
    except ImportError:
        raise DirectoryFailure("DIRECTORY_CLIENT_UNAVAILABLE") from None


@sensitive_variables()
def _bind_password(filename):
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                or info.st_uid not in {0, os.getuid()} or not 0 < info.st_size <= 4096):
            raise DirectoryFailure("DIRECTORY_SECRET_UNAVAILABLE")
        value = os.read(fd, 4097).decode("utf-8").rstrip("\r\n")
    finally:
        os.close(fd)
    if not value or len(value) > 4096 or any(c in value for c in "\x00\r\n"):
        raise DirectoryFailure("DIRECTORY_SECRET_UNAVAILABLE")
    return value


def _attributes(raw):
    if not isinstance(raw, dict) or len(raw) > 32:
        raise DirectoryFailure("DIRECTORY_RECORD_INVALID")
    result = {}
    for key, values in raw.items():
        if not isinstance(key, str) or key.lower() in result or not isinstance(values, list) or len(values) > 8:
            raise DirectoryFailure("DIRECTORY_RECORD_INVALID")
        decoded = []
        for value in values:
            if not isinstance(value, bytes) or len(value) > 8192:
                raise DirectoryFailure("DIRECTORY_RECORD_INVALID")
            text = value.decode("utf-8", errors="strict")
            if any(ord(c) < 32 or ord(c) == 127 for c in text):
                raise DirectoryFailure("DIRECTORY_RECORD_INVALID")
            decoded.append(text)
        result[key.lower()] = decoded
    return result


def _single(attributes, name):
    values = attributes.get(name.lower(), [])
    if len(values) != 1 or not values[0]:
        raise DirectoryFailure("DIRECTORY_RECORD_INVALID")
    return values[0]


def _subject(value):
    result = UUID(value)
    if result.int == 0 or str(result) != value.lower():
        raise DirectoryFailure("DIRECTORY_SUBJECT_INVALID")
    return str(result)


def _status(attributes, policy, now):
    # These checks are opt-in after the operator confirms the site's semantics.
    # Samba's D/L meanings: https://www.samba.org/samba/docs/current/man-html/pdbedit.8.html
    if policy.get("samba_account_flags"):
        flags = _single(attributes, "sambaAcctFlags")
        if not re.fullmatch(r"\[[NDHTUMWSLXI ]{1,32}\]", flags):
            raise DirectoryFailure("DIRECTORY_STATUS_UNKNOWN")
        if "D" in flags or "L" in flags:
            return False
    if policy.get("shadow_expire"):
        value = _single(attributes, "shadowExpire")
        if not re.fullmatch(r"-1|[1-9][0-9]{0,9}", value):
            # Zero has conflicting interpretations; absence is not asserted as
            # never-expiring when a site explicitly requires this check.
            raise DirectoryFailure("DIRECTORY_STATUS_UNKNOWN")
        expiry = int(value)
        if expiry != -1 and int(now.timestamp() // 86400) >= expiry:
            return False
    return True


class LDAPAdapter:
    def __init__(self, configuration):
        self.configuration = configuration

    @sensitive_variables()
    def _connect(self, library, password):
        config = self.configuration
        parsed = urlsplit(config["server_uri"])
        tls = library.Tls(validate=ssl.CERT_REQUIRED, version=None, ca_certs_file=config["ca_cert_file"],
            valid_names=[parsed.hostname], sni=parsed.hostname)
        server = library.Server(parsed.hostname, port=parsed.port or (636 if parsed.scheme == "ldaps" else 389),
            use_ssl=parsed.scheme == "ldaps", tls=tls, get_info=library.NONE,
            connect_timeout=config["network_timeout_seconds"], allowed_referral_hosts=[])
        connection = library.Connection(server, user=config["bind_dn"], password=password,
            version=3, authentication=library.SIMPLE, client_strategy=library.SYNC, auto_bind=library.AUTO_BIND_NONE,
            auto_referrals=False, auto_range=False, read_only=True, check_names=False, collect_usage=False,
            lazy=False, raise_exceptions=True, receive_timeout=config["operation_timeout_seconds"],
            return_empty_attributes=False, use_referral_cache=False)
        try:
            connection.open(read_server_info=False)
            if connection.closed:
                raise DirectoryFailure()
            if config["start_tls"] and not connection.start_tls(read_server_info=False):
                raise DirectoryFailure()
            if not connection.bind(read_server_info=False):
                raise DirectoryFailure()
            return connection
        except Exception:
            self._close(connection)
            raise DirectoryFailure() from None

    @staticmethod
    def _close(connection):
        try:
            connection.unbind()
        except Exception:
            pass
        # ldap3 keeps bind material in its connection object; it is never cached.
        connection.password = None
        connection.user = None
        connection.request = None
        connection.response = None

    def _search(self, library, connection, base, scope, filter_string, attributes):
        connection.search(base, filter_string, search_scope=scope, attributes=attributes,
            size_limit=2, time_limit=self.configuration["operation_timeout_seconds"],
            dereference_aliases=library.DEREF_NEVER)
        if not isinstance(connection.result, dict) or connection.result.get("result") != 0:
            raise DirectoryFailure()
        rows = connection.response
        if not isinstance(rows, list) or len(rows) > 2 or any(not isinstance(row, dict) or row.get("type") != "searchResEntry" for row in rows):
            raise DirectoryFailure("DIRECTORY_RECORD_INVALID")
        if not rows:
            raise DirectoryFailure("SUBJECT_NOT_FOUND")
        if len(rows) != 1:
            raise DirectoryFailure("DIRECTORY_RECORD_AMBIGUOUS")
        row = rows[0]
        dn = row.get("dn")
        if not isinstance(dn, str) or not dn or len(dn) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in dn):
            raise DirectoryFailure("DIRECTORY_RECORD_INVALID")
        return dn, _attributes(row.get("raw_attributes"))

    def _requested_attributes(self):
        config = self.configuration
        attributes = {config["subject_attribute"], config["username_attribute"], *config["profile_attributes"].values()}
        if config["status_policy"].get("shadow_expire"):
            attributes.add("shadowExpire")
        if config["status_policy"].get("samba_account_flags"):
            attributes.add("sambaAcctFlags")
        return sorted(attributes)

    def _principal(self, library, connection, attributes):
        from ldap3.utils.conv import escape_filter_chars
        config = self.configuration
        subject = _subject(_single(attributes, config["subject_attribute"]))
        login_name = _single(attributes, config["username_attribute"])
        if len(login_name) > 150:
            raise DirectoryFailure("DIRECTORY_RECORD_INVALID")
        active = _status(attributes, config["status_policy"], datetime.now(timezone.utc))
        policy = config["status_policy"]
        if active and policy.get("required_group_dn"):
            field = policy.get("group_membership_attribute", "memberUid")
            object_class = policy.get("group_object_class", "posixGroup")
            try:
                _, group = self._search(library, connection, policy["required_group_dn"], library.BASE,
                    f"(&(objectClass={object_class})({field}={escape_filter_chars(login_name)}))", [config["subject_attribute"]])
                if policy.get("required_group_subject") and _subject(_single(group, config["subject_attribute"])) != policy["required_group_subject"]:
                    active = False
            except DirectoryFailure as error:
                if error.code != "SUBJECT_NOT_FOUND":
                    raise
                active = False
        profile = {}
        limits = {"first_name": 150, "last_name": 150, "email": 254, "display_name": 255}
        for field, attribute in config["profile_attributes"].items():
            values = attributes.get(attribute.lower(), [])
            profile[field] = sorted(values)[0][:limits[field]] if values else ""
        return DirectoryPrincipal(subject, login_name, active=active, **profile)

    @sensitive_variables()
    def authenticate(self, username, password):
        connection = None
        try:
            if (not isinstance(username, str) or not 0 < len(username) <= 150 or any(ord(c) < 32 for c in username)
                    or not isinstance(password, str) or not 0 < len(password) <= 4096 or "\x00" in password):
                raise DirectoryFailure("LOGIN_REJECTED")
            from ldap3.utils.conv import escape_filter_chars
            library = _ldap()
            bind_password = _bind_password(self.configuration["bind_password_file"])
            connection = self._connect(library, bind_password)
            config = self.configuration
            dn, initial = self._search(library, connection, config["user_base_dn"], library.SUBTREE,
                f"(&(objectClass={config['object_class']})({config['username_attribute']}={escape_filter_chars(username)}))", self._requested_attributes())
            initial_subject = _subject(_single(initial, config["subject_attribute"]))
            if not connection.rebind(user=dn, password=password, authentication=library.SIMPLE, read_server_info=False):
                raise DirectoryFailure("LOGIN_REJECTED")
            # Read the same stable subject after successful user bind. A deleted/
            # recreated DN must not authenticate the previous subject's account.
            _, verified = self._search(library, connection, dn, library.BASE,
                f"(objectClass={config['object_class']})", self._requested_attributes())
            if _subject(_single(verified, config["subject_attribute"])) != initial_subject:
                raise DirectoryFailure("DIRECTORY_SUBJECT_CHANGED")
            if config["status_policy"].get("required_group_dn"):
                if not connection.rebind(user=config["bind_dn"], password=bind_password, authentication=library.SIMPLE, read_server_info=False):
                    raise DirectoryFailure()
            return self._principal(library, connection, verified)
        except DirectoryFailure:
            raise
        except Exception:
            raise DirectoryFailure() from None
        finally:
            if connection is not None:
                self._close(connection)

    @sensitive_variables()
    def lookup_subject(self, subject):
        connection = None
        try:
            # A service search alone does not prove password-bind eligibility.
            # Without an explicit site status policy, require a fresh login.
            if not any(self.configuration["status_policy"].get(field) for field in ("shadow_expire", "samba_account_flags", "required_group_dn")):
                raise DirectoryFailure("STATUS_POLICY_REQUIRED")
            subject = _subject(subject)
            from ldap3.utils.conv import escape_filter_chars
            library = _ldap()
            bind_password = _bind_password(self.configuration["bind_password_file"])
            connection = self._connect(library, bind_password)
            config = self.configuration
            _, attributes = self._search(library, connection, config["user_base_dn"], library.SUBTREE,
                f"(&(objectClass={config['object_class']})({config['subject_attribute']}={escape_filter_chars(subject)}))", self._requested_attributes())
            principal = self._principal(library, connection, attributes)
            if principal.subject != subject:
                raise DirectoryFailure("DIRECTORY_SUBJECT_CHANGED")
            return principal
        except DirectoryFailure:
            raise
        except Exception:
            raise DirectoryFailure() from None
        finally:
            if connection is not None:
                self._close(connection)
