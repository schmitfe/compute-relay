# Synthetic LDAP for development

This is an opt-in OpenLDAP server populated from repository-owned synthetic
records. It does not import a NAS export. The standard `inetOrgPerson`,
`posixAccount`, `shadowAccount` and `posixGroup` schemas are combined with a
minimal test-only `sambaSamAccount`/`sambaAcctFlags` schema. Do not use that
minimal Samba schema as a production directory schema.

The users `alice` and `bob` are eligible members of the `allowed` POSIX group.
`disabled` has the Samba disabled flag; `expired` has an expired shadow date.
Every account has an independently generated password. Passwords are written
only to private `0600` files, and directory password hashes are generated
inside the fixture. OpenLDAP generates `entryUUID` values. The fixture never
prints passwords or places them in command arguments. Application identities
are created at successful first login without project, worker, or cluster
access grants.

From the repository root, build and run the finite native acceptance:

```sh
docker compose -p broker-ldap -f deploy/ldap/compose.yml build
docker compose -p broker-ldap -f deploy/ldap/compose.yml up -d --wait ldap
docker compose -p broker-ldap -f deploy/ldap/compose.yml run --rm acceptance
```

The acceptance client always creates its own temporary SQLite database, with
no inherited portal DB setting. It tests native LDAPS, mandatory StartTLS,
wrong CA and hostname rejection, local/LDAP same-name separation, eligible and
ineligible accounts, expiry refresh and preserved operator suspension. The
default network has no published ports. Named volumes persist the directory,
CA and generated passwords across restarts; `down -v` deliberately destroys
the synthetic identities and secrets.

For the local portal, add `-f deploy/ldap/compose.portal.yml` to the same
Compose commands. This publishes only `127.0.0.1:1636`, with a certificate
valid for `localhost` and `127.0.0.1`. This overlay adds a separate bridge for
host port publishing; the default acceptance profile keeps only its internal
fixture network. Keep the server running while using the mock provider.
Export the public CA and selected generated password files
into an ignored, private local directory using Compose's file-copy command;
do not print them into logs. Example, after creating a `0700` destination:

```sh
docker compose -p broker-ldap -f deploy/ldap/compose.yml cp ldap:/run/ldap-fixture/ca.pem local/mock-ldap/ca.pem
docker compose -p broker-ldap -f deploy/ldap/compose.yml cp ldap:/run/ldap-fixture/reader-password local/mock-ldap/reader-password
docker compose -p broker-ldap -f deploy/ldap/compose.yml cp ldap:/run/ldap-fixture/alice-password local/mock-ldap/alice-password
```

Copy preserves each password file's private mode; ensure the portal service
owns the copied reader file. The application must use its sole identity
router backend. Set `BROKER_IDENTITY_CONFIG_FILE` to a protected file with
the local provider plus a mock LDAP provider configured as follows:

```json
{
  "local": {"kind": "local", "label": "Local account", "enabled": true},
  "mock": {
    "kind": "ldap", "label": "Synthetic LDAP", "enabled": true,
    "server_uri": "ldaps://localhost:1636", "start_tls": false,
    "ca_cert_file": "/absolute/private/mock-ldap/ca.pem",
    "bind_dn": "cn=broker-reader,dc=fixture,dc=invalid",
    "bind_password_file": "/absolute/private/mock-ldap/reader-password",
    "user_base_dn": "ou=people,dc=fixture,dc=invalid",
    "status_policy": {
      "shadow_expire": true, "samba_account_flags": true,
      "required_group_dn": "cn=allowed,ou=groups,dc=fixture,dc=invalid",
      "group_object_class": "posixGroup", "group_membership_attribute": "memberUid"
    }
  }
}
```

The fixture server's private CA key stays in its private volume. Leaf
certificates expire after 90 days; reset the disposable fixture volumes to
regenerate them and update the portal's trusted CA copy. A reset creates new
directory entryUUIDs and therefore new application identities. For a real
directory, use an operator-managed trust chain and lifecycle instead.

For a fixture certificate-format upgrade, preserve both named volumes and
stop the server first. Remove only `ca.pem` from the shared certificate volume
using a controlled one-off container, then start the rebuilt server image.
The certificate initializer creates a fresh CA and leaf chain when that file
is absent; it leaves the LDAP database and generated password files intact.
Export the replacement public CA to the portal before testing login again.
This procedure preserves directory entryUUIDs and application identity links;
it is different from resetting the disposable fixture volumes.
