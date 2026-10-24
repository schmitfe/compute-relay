# Archived shared-server design

The active product is a single-user package with a loopback portal, local owner,
SQLite ledger and device-local SSH connectors. See [architecture](architecture.md)
and [runtime](runtime.md).

The previous hosted/multi-user design, including LDAP, PostgreSQL, memberships,
server deployment and transfer tools, is retained on
`codex/archive-multi-user-server` at commit `520effc`. Those capabilities are
outside the current installation contract and have no ordinary dependency extra.
No migration or live server modification is implied by this change in direction.
