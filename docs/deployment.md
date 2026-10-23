# Deployment and operations

The development alpha includes native and synthetic container fixtures, SQLite and PostgreSQL configuration, an offline database-transfer tool, explicit local/LDAP login and an opt-in asynchronous core Slurm worker. See [runtime configuration](runtime.md) and [container testing](container-testing.md). These implemented components do not establish a protected shared installation. The current settings accept only `development`; protected profiles remain refused until their installation tests are complete.

The [shared-service architecture](hosted-architecture.md) distinguishes the
implemented database, identity and worker foundations from the remaining hosted
deployment work. The design prohibits central user SSH private-key storage. Its
isolated server-session mode still needs a tested keyless activation mechanism;
ordinary Compose packaging alone does not establish that capability or tenant
isolation.

## Supported profiles

| Profile | Intended use | Credential isolation |
| --- | --- | --- |
| `development` | Implemented same-user checkout and test fixtures; separately authorized supervised SSH tests | No protection from an agent under that user; routine acceptance uses synthetic credentials |
| `personal_protected` | Planned installation for one owner on supported Linux | Requires administrator-managed identities, protected runtime/configuration and private worker sockets; not selectable yet |
| `server` | Planned protected shared Linux installation | PostgreSQL and local/LDAP foundations exist; HTTPS operations, isolated connectors and production acceptance remain; not selectable yet |

Docker Compose is the packaging target for the protected profiles. An administrator installs a reviewed image/configuration outside the agent-writable checkout and manages runtime access. A dedicated service-owned rootless runtime is also possible if its control socket is inaccessible to the agent. Do not expose an installation as protected merely because a container runs as non-root. No container runtime runs on Slurm login/compute nodes as a broker prerequisite; remote Python 3 and the existing Slurm CLI are the initial helper prerequisites.

The first protected host target is Linux with OpenSSH. Windows/macOS support is deferred until service identity or restricted-VM operation and installation tests are specified. A user who cannot perform a one-time privileged setup can use development mode with a fake backend, or connect an agent-only facade to a broker hosted elsewhere.

## Containers and protected deployment target

The repository supplies control/worker Docker targets and disposable fake,
Slurm, PostgreSQL and LDAP fixtures. Their commands and evidence are in
[container testing](container-testing.md), [database migration](database-migration.md)
and the [LDAP fixture guide](../deploy/ldap/README.md). Install the `postgres` or
`ldap` dependency extra when enabling that adapter; the minimal control image
does not implicitly enable them. The following layout describes the protected
deployment target, rather than a completed production installer.

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
| Authentication | Implemented local/LDAP provider routing and protected LDAP configuration; project memberships and approval channel policy; OIDC remains future work |
| Database/storage | Protected local paths or PostgreSQL secret reference, object store adapter |
| Workers | Worker IDs, enrollment trust, assigned connection IDs, lease/timeouts and outbound target allowlist |
| Connections | Host/port/user, pinned host key/CA, service key reference, protected ControlPath, absolute Slurm/Python paths, working root |
| Site accounting | Supported Slurm version, rounding/allocation behavior and final allocated-runtime evidence; automatic mode capability; reservations use requested walltime |

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

The implemented `IdentityRoutingBackend` is the sole Django authentication
backend. Local login remains the default. An optional `ldap3` adapter supports
verified LDAPS or required StartTLS, explicit provider selection and immutable
provider/`entryUUID` identity binding. A directory failure never selects a local
account with a matching name. First LDAP login creates no project membership,
worker or HPC access. See [identity configuration](../src/compute_broker/control/identity/README.md)
and the [synthetic TLS fixture](../deploy/ldap/README.md); no institutional
directory acceptance is implied by that fixture.

Directory authority is cached for a configured interval of at most 300 seconds.
Expired verification or directory denial blocks new authority, including use of
the user's existing project keys and dispatch. The optional protected refresher
does network I/O outside database transactions and cannot clear operator
suspension. Current eligibility checks support configured POSIX group membership,
Samba flags and shadow expiry. Automatic directory-to-project role mapping,
membership-management screens, login rate limiting and OIDC remain unfinished.

Project keys are bound to their issuing users. Current project role and account
eligibility are checked on key use and again at dispatch. An executable connection
and its worker must belong to that same user; a shared project allowance does not
delegate another member's SSH identity. Only the issuer can rotate and reveal a
replacement execution key. Membership and cross-member resource grants currently
need reviewed operator management. LDAP login alone does not grant SSH access.

Protected personal connectors would use outbound authenticated HTTPS to the
authoritative control service. They do not admit jobs while disconnected. Batch
jobs already submitted continue; accounting reservations remain until
reconciliation. A central per-user session supervisor and keyless MFA activation
still require implementation and site acceptance.

Remote HTTP MCP also needs a standards-compliant OAuth resource-server integration, scoped project selection and actual ChatGPT/Claude/Codex client tests. Do not assume an OIDC web login or a raw project API header is compatible with every hosted client. No new OAuth authorization-server implementation is part of the first local milestone.

## Recovery and maintenance

Expose liveness, private readiness, connection authentication state, age of latest successful discovery/accounting, outstanding reservations, uncertain submissions, task lease lag, and failed artifact transfers. Redact secrets and avoid institutional data in metric labels. Poll scheduler commands in batches with bounded concurrency/backoff; agents do not cause one scheduler RPC for every status request.

Routine maintenance includes key/session rotation, admission pause, cancellation reconciliation, retention/garbage collection, schema migrations and image updates. Garbage collection never deletes packages/evidence of unsettled attempts. Backups include SQL, objects, trusted configuration and independently protected secret material. Exported backups require encryption and retention controls.

Restore starts with admissions paused. Verify database/object consistency, reconcile every outstanding native identity and remote submission UUID, and resolve any jobs that were accepted after the backup snapshot. A restore cannot safely assume a missing local record means no remote job. Do not resume after a stale backup by clearing reservations. Preserve accounting evidence and record operator adjustments explicitly.

The implemented [offline SQLite-to-PostgreSQL procedure](database-migration.md)
requires stopped writers, paused projects, resolved work, an immutable source
backup and an empty migrated target. It checks model/through-row counts,
canonical digests, ledger/storage balances and artifact bytes, preserving UUIDs,
password hashes and session rows. Keep the original protected Django secret.
Changing the Django authentication backend path nevertheless requires browser
sign-in again. Legacy keys without proven issuing users remain quarantined until
an operator explicitly verifies their owner; a sole current member is not proof.
The tool never changes the live database configuration itself.

A future protected local-to-hosted cutover also needs reviewed identity linking
and connector re-enrollment into the new isolation boundary. Run one authoritative
ledger; rollback after new activity requires reconciliation rather than starting
a second writable controller. A copied-data rehearsal does not certify full
disaster recovery or hosted production operations.

The [async runner](async-workers.md) is opt-in and supports core Slurm only. It
bounds HTTP/SSH operations and separates owner lanes, while keeping existing
leases, journals and accounting. Optional extensions still use the default
synchronous runner. Process concurrency is not an operating-system isolation
boundary or a substitute for protected connector installation.

Uninstall stops admissions first, reconciles or explicitly hands off active jobs, exports retained data if requested, closes SSH masters and revokes worker/project credentials. Removing containers does not cancel Slurm jobs or revoke remote public keys. Delete protected data only through an explicit operator action after reconciling unsettled usage.

## Public demonstration

The native fake demonstration and disposable Docker fake/SSH-Slurm workflows are
implemented. Separate PostgreSQL and TLS LDAP fixtures test those adapters with
synthetic identities. Ordinary CI uses no institutional credentials or remote
cluster allocations. Demonstration passwords and keys are generated at runtime
and must not become protected-installation credentials.

Dockerfiles and Compose fixtures provide reproducible development acceptance.
They do not yet provide the protected installer, independent human approval
channel, institutional Cisco/MFA activation or full hosted production acceptance.
