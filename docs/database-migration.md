# Former server database migration

The current package uses embedded SQLite and one local owner. PostgreSQL
configuration, cross-backend transfer tools, LDAP identity transfer and legacy
multi-user key-owner mapping are no longer part of the supported runtime.

Their implementation and documentation are preserved on
`codex/archive-multi-user-server` at commit `520effc`. Inspect that branch if
maintaining the former server design; do not apply its commands to a running
local installation as an automatic upgrade path.

Normal local backups and recovery are described in [installation](deployment.md).
Source cleanup does not modify or import any existing server data.
