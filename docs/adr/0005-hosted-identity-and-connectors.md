# Hosted identity, PostgreSQL and async per-user connectors

Status: accepted implementation direction; not implemented hosted support.
Date: 2026-10-22.

## Context

The development service already has Django user/project records, hashed local
passwords, project API keys and a worker boundary. Its runtime is explicitly a
same-account development deployment with SQLite and serial SSH worker operations.
Adding a shared login page would not isolate users' HPC credentials or sessions.
The requested hosted design supports local or LDAP authentication and prohibits
central storage of user SSH private keys.

## Decision

1. Keep Django and stable internal user UUIDs. Support explicitly routed local
   authentication and an optional LDAP adapter, with unique provider/immutable
   subject links. LDAP-only users have unusable local passwords. Failed LDAP
   authentication never falls through to a same-username local account.
2. Use PostgreSQL for the shared service and CI integration, retaining SQLite
   for personal/development use. Test backend-specific locking; migrate history,
   not identities or ledgers invented afresh. No live cutover occurs merely by
   accepting this decision.
3. Separate application user, project-scoped agent credential and verified HPC
   principal. Project membership is not delegation of another user's HPC account.
   Principal ownership/delegation and directory status are rechecked at dispatch.
4. Store no HPC private keys centrally, including encrypted vaults. The broker
   has no SSH client, agent or control socket. Use isolated per-principal
   connectors with a narrow typed outbound lease/report protocol.
5. A server connector may hold a site-approved, expiring authenticated SSH session
   under a distinct OS security identity. Treat the session socket as a sensitive
   capability. Require an actually tested keyless bootstrap mechanism; do not
   assume a local SSH session can be copied to the server. Ordinary agent
   forwarding is disabled in the baseline. Until bootstrap/isolation are accepted,
   use an outbound personal connector with credentials on a protected user device.
6. Convert cluster/network operations to bounded async I/O using fixed OpenSSH
   subprocesses, an async HTTP client and durable lease/journal fencing. Keep
   transactional admission/accounting in short synchronous Django functions.
   No network await occurs inside a ledger transaction. Async is not a reason
   to replace reviewed authorization code or create unlimited scheduler polling.
7. Directory deprovisioning has an explicit bounded freshness window and blocks
   new authority, including relevant agent keys. Reconciliation of dispatched
   work remains possible without restoring the revoked user's output access.

## Consequences

Hosted deployment needs additional principal/session models, identity routing,
PostgreSQL settings/tests, async workers and an isolated activation supervisor.
No generic terminal/SSH tool is added. LDAP bind passwords are transient login
inputs, never cluster credentials. Site API delegation remains a future backend.

A central session still permits remote account actions while it is alive and
is vulnerable to its host administrator/connector compromise. User-private-key
custody and session authority are separate guarantees. Different processes or
containers under one unprotected identity do not establish tenant isolation.

The [implementation specification](../hosted-architecture.md) defines models,
deprovisioning, bootstrap limits, migration sequence, repository work packages
MU-01 through MU-06 and acceptance tests. It refines older OIDC-first planning
to make requested direct LDAP and local authentication explicit; OIDC remains
an optional later provider. Current runtime limitations remain in force.
