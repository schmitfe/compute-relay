# Human identity providers

The application uses a single `IdentityRoutingBackend`. An explicitly selected
LDAP provider never falls back to a local password. Existing local accounts
remain local; an external account is linked only by provider ID and canonical
OpenLDAP `entryUUID`, never by username, email address, or distinguished name.
Do not rename a provider ID to migrate a directory: that creates a new identity
namespace. LDAP users receive opaque local usernames, unusable local passwords,
and `local_login_allowed=False`.

Set `BROKER_IDENTITY_CONFIG_FILE` to an absolute, protected JSON configuration
file. Omission enables only the local provider. Install the optional `ldap`
dependency to enable directory providers. Configuration contains the path to a
service bind password file, never the password itself. The password file must
be a single-link regular file owned by the service user or root, mode `0600`
or stricter. The service account must be allowed to read the selected nonsecret
identity and eligibility attributes.

```json
{
  "local": {"kind": "local", "label": "Local account", "enabled": true},
  "mock": {
    "kind": "ldap", "label": "Synthetic directory", "enabled": true,
    "server_uri": "ldaps://ldap-fixture:1636", "start_tls": false,
    "ca_cert_file": "/run/identity/ca.pem",
    "bind_dn": "cn=broker-reader,dc=fixture,dc=invalid",
    "bind_password_file": "/run/identity/reader-password",
    "user_base_dn": "ou=people,dc=fixture,dc=invalid",
    "username_attribute": "uid", "subject_attribute": "entryUUID",
    "object_class": "inetOrgPerson", "freshness_seconds": 300,
    "status_policy": {
      "shadow_expire": true, "samba_account_flags": true,
      "required_group_dn": "cn=allowed,ou=groups,dc=fixture,dc=invalid",
      "group_object_class": "posixGroup",
      "group_membership_attribute": "memberUid"
    }
  }
}
```

Only verified LDAPS or mandatory StartTLS is accepted. Referrals and anonymous
or empty-password binds are disabled; searches, connection time and operation
time have finite limits. The current group implementation supports POSIX
`memberUid` membership only; DN-valued `member` and `uniqueMember` are rejected.
Samba disabled/lock flags and shadow expiry are opt-in policies. Missing or
ambiguous configured status is denied. The adapter does not import directory
password hashes, staff roles, project memberships, or cluster credentials.

External authority expires after at most 300 seconds, shortened by provider
configuration. Config changes invalidate old verification. The pure database
`cached_user_active()` check is suitable inside admission and dispatch
transactions. Directory network requests must occur outside transactions.
An operator can run `python -m compute_broker.control.identity.refresh --limit
20 --interval 30` as a protected service to refresh due identities. This
process renews authority only when an explicit status or group policy allows
a service-account lookup to establish eligibility. Without that policy,
interactive login is required again after freshness expires. Outages do not
extend freshness, and directory refresh never re-enables a globally suspended
user or an operator-disabled identity link.

At deployment cutover from Django's `ModelBackend`, existing browser sessions
must sign in again because the stored backend path changes. Session database
rows can remain intact; that does not preserve authentication through a
different provider backend. Do not append unrestricted `ModelBackend` as a
compatibility fallback. Existing issued project keys are additionally subject
to the application's issuer, membership and cluster-owner authorization rules.

The disposable mock directory under `deploy/ldap` is independent of any real
NAS export. Its users and groups are synthetic. Real institutional LDAP stays
disabled until an operator supplies an endpoint, trusted CA and explicit
eligibility policy.
