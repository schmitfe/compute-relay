# Deployment and operations plan

Only the development alpha and synthetic container fixtures are currently implemented. See [runtime configuration](runtime.md) and [container testing](container-testing.md). Protected profiles in this plan are refused by the current settings until their installation tests are complete.

The [shared-service specification](hosted-architecture.md) now defines the
local/LDAP identity model, PostgreSQL cutover, async connector implementation and
prohibition on central user SSH private-key storage. Its isolated session mode
needs a tested keyless activation mechanism; ordinary Compose packaging alone
does not establish that capability or tenant isolation.

## Supported profiles

| Profile | Intended use | Credential isolation |
| --- | --- | --- |
| `development` | Same-user source checkout, fake backend, synthetic data | No claim of protection from an agent under that user; real SSH credentials disabled |
| `personal_protected` | One owner on a supported Linux device | Administrator-managed service identities, protected runtime/configuration, private worker sockets |
| `server` | Shared Linux server and organizational users | PostgreSQL, HTTPS, explicit local/LDAP providers, project roles and isolated per-principal connectors |

Docker Compose is the packaging target for the protected profiles. An administrator installs a reviewed image/configuration outside the agent-writable checkout and manages runtime access. A dedicated service-owned rootless runtime is also possible if its control socket is inaccessible to the agent. Do not expose an installation as protected merely because a container runs as non-root. No container runtime runs on Slurm login/compute nodes as a broker prerequisite; remote Python 3 and the existing Slurm CLI are the initial helper prerequisites.

The first protected host target is Linux with OpenSSH. Windows/macOS support is deferred until service identity or restricted-VM operation and installation tests are specified. A user who cannot perform a one-time privileged setup can use development mode with a fake backend, or connect an agent-only facade to a broker hosted elsewhere.

## Planned container layout

| Service | Mounts | Connectivity |
| --- | --- | --- |
| `control` | SQL data locally, immutable package/artifact store, protected Django secret and worker trust configuration | Public application listener behind reverse proxy; private worker listener; SQL when configured |
| `worker` | Assigned connector token, pinned configuration, private session sockets and bounded staging/cache; user private keys allowed only on protected personal devices, never central hosted runner storage | Outbound control API and allowlisted cluster login nodes; no database connection |
| `postgres` | Dedicated server database volume and secret file | Private control network only; omitted for local SQLite profile |
| `proxy` | Public TLS configuration and certificates | Published application/admin endpoints according to policy; no cluster keys |

The local stdio MCP facade is a client program, not a privileged container. HTTP MCP is a later independently restricted facade. Worker uploads/downloads use scoped control endpoints; object-store credentials are not mounted into the agent facade.

Container requirements: pinned release images, non-root application UIDs, read-only root filesystems, bounded tmpfs/scratch, dropped capabilities, `no-new-privileges`, runtime seccomp/AppArmor where supported, no host PID namespace, no Docker socket, no unrestricted devices/home mounts, and no agent-supplied Compose fragments. Containers receive only needed secret files. A source bind mount or editable configuration that a desktop agent can change invalidates the security profile.

Do not use default Compose flat networking as the authorization boundary. The worker API validates identity and connection scope even on private networks. Public exposure of the worker port, database, MFA helper, or alternative backend/gateway is prohibited. Loopback still requires authentication and Host/Origin checks. TLS is mandatory for remote access; a local administrator configures the personal UI's trusted origin and certificate strategy.

## Configuration

[Personal](../examples/personal.toml) and [server](../examples/server.toml) examples define proposed settings, not runnable deployment configuration. The configuration loader will validate unknown keys strictly and classify each value as public, private metadata or secret reference. Project API bodies cannot override trusted settings.

| Setting family | Administrator-controlled values |
| --- | --- |
| Application | Deployment profile, canonical URL/origins, timezone UTC internally, listener addresses, limits, retention |
| Storage quotas | Per-project/global bytes and object counts, upload concurrency/reservations, orphan TTL and minimum disk headroom |
| Authentication | Local owner or trusted OIDC issuer/client, verified group mapping, approval channel policy |
| Database/storage | Protected local paths or PostgreSQL secret reference, object store adapter |
| Workers | Worker IDs, enrollment trust, assigned connection IDs, lease/timeouts and outbound target allowlist |
| Connections | Host/port/user, pinned host key/CA, service key reference, protected ControlPath, absolute Slurm/Python paths, working root |
| Site accounting | Supported Slurm version, rounding/allocation behavior, finite termination margin and evidence; automatic mode capability |

Connection settings are immutable revisions. Editing host, username, scheduler binaries, working root, host-key trust or allocation model invalidates prepared packages using the prior target configuration. Routine liveness/idle-resource observations do not invalidate otherwise identical reviewed bytes.

## Personal bootstrap

The future installer performs these steps from a trusted administrator session:

1. Create protected identities/directories under `/opt/compute-broker`, `/etc/compute-broker`, `/var/lib/compute-broker`, and `/run/compute-broker`; verify the agent cannot traverse secret directories, switch identity or control the daemon.
2. Verify the release image, set explicit origins/listeners, initialize schema migrations and protected storage. Generate fresh control/worker secrets internally; never print them to general logs.
3. Create a one-time owner enrollment through a protected channel. Close enrollment after use; there is no default password or permanent setup token.
4. Create a project and admission policy. Issue separate scoped agent credentials, display once, and store their hashes. No project credential can approve or administer the installation.
5. Enroll each cluster connection. Generate/import keys through the protected service identity; register public keys through the cluster's normal procedure and explicitly verify the host key.
6. Complete the university's human SSH MFA in the protected connector context. Do not reuse an agent-readable desktop SSH socket. Confirm expiry/reconnect behavior without unattended MFA retries.
7. Run synthetic local isolation checks, then opt-in tiny real-cluster acceptance with explicit account/partition/resource caps. Enable automatic budgeted mode only after its allocation/accounting model is verified.

Strict independent human approval needs a trusted session/device that the agent cannot operate or observe. An owner browser on an agent-controlled desktop is suitable only for supervised convenience mode with that limitation shown. Hardware touch alone is not proof of reviewing the displayed transaction on a compromised desktop.

## Shared server and directory login

Deploy the same control application against PostgreSQL and organizational OIDC. Prefer the organization's existing identity provider; LDAP federation can live in Keycloak or a comparable existing service rather than requiring the broker to handle directory passwords. A direct LDAP adapter is a later alternative, not a personal-installation requirement. Configure audience/issuer validation, group-to-project role rules, deprovisioning and session expiry explicitly. Stable issuer/subject pairs identify users. [Keycloak LDAP federation](https://www.keycloak.org/docs/latest/server_admin/index.html)

Per-user cluster connections retain independent identities. The server may host connections for approved service identities, or pair a protected worker on a user's device over outbound authenticated HTTPS. LDAP login alone does not grant SSH access. A personal connector does not admit jobs while disconnected from the authoritative control service. Batch jobs already submitted continue; accounting reservations remain until reconciliation.

Remote HTTP MCP also needs a standards-compliant OAuth resource-server integration, scoped project selection and actual ChatGPT/Claude/Codex client tests. Do not assume an OIDC web login or a raw project API header is compatible with every hosted client. No new OAuth authorization-server implementation is part of the first local milestone.

## Recovery and maintenance

Expose liveness, private readiness, connection authentication state, age of latest successful discovery/accounting, outstanding reservations, uncertain submissions, task lease lag, and failed artifact transfers. Redact secrets and avoid institutional data in metric labels. Poll scheduler commands in batches with bounded concurrency/backoff; agents do not cause one scheduler RPC for every status request.

Routine maintenance includes key/session rotation, admission pause, cancellation reconciliation, retention/garbage collection, schema migrations and image updates. Garbage collection never deletes packages/evidence of unsettled attempts. Backups include SQL, objects, trusted configuration and independently protected secret material. Exported backups require encryption and retention controls.

Restore starts with admissions paused. Verify database/object consistency, reconcile every outstanding native identity and remote submission UUID, and resolve any jobs that were accepted after the backup snapshot. A restore cannot safely assume a missing local record means no remote job. Do not resume after a stale backup by clearing reservations. Preserve accounting evidence and record operator adjustments explicitly.

SQLite-to-PostgreSQL migration uses explicit export/import commands, row counts, digests, referential integrity and ledger reconstruction checks; it is not a database URL toggle. Local-to-hosted migration fences the old controller, transfers data, links the owner to organizational identity through a trusted account-link operation, re-pairs workers and resumes one authoritative ledger. Rollback requires reconciliation, not simultaneous writable controllers.

Uninstall stops admissions first, reconciles or explicitly hands off active jobs, exports retained data if requested, closes SSH masters and revokes worker/project credentials. Removing containers does not cancel Slurm jobs or revoke remote public keys. Delete protected data only through an explicit operator action after reconciling unsettled usage.

## Public demonstration

Provide a fake-backend demonstration with generated synthetic identities and no real cluster network access. A separate ephemeral SSH/Slurm integration fixture may use Docker on an isolated test runner, never the production daemon. Demonstration passwords/keys must be generated at runtime and must not be accepted by protected installations.

No actual Dockerfile, Compose deployment or install command is claimed by this planning repository. Milestone M1 produces and verifies the fake deployment; protected deployment follows its OS isolation tests.
