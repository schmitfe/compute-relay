# Archived hosted identity and database direction

Status: superseded by the single-user package scope.

The former local/LDAP identity, memberships, PostgreSQL and shared-server plan is
preserved on `codex/archive-multi-user-server` at commit `520effc`.
The active runtime has one local owner, SQLite, a loopback portal and local SSH
connectors. Reusing an execution backend or optional module does not reintroduce
shared-server identity or database dependencies.
