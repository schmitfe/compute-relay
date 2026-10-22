# Security architecture and threat model

Status: proposed implementation requirements, not a claim that this repository
already enforces them. External capabilities were checked against primary
documentation on 2026-10-21. Cluster-specific behavior still needs acceptance
testing. This document defines the security contract for both personal and
hosted deployments.

## 1. Supported threat model and guarantees

Assume the agent can read and modify its user's files, run arbitrary unprivileged
programs, inspect that user's processes, access its browser session, and submit
malicious scripts. Treat prompt injection and incorrect model output as ordinary
untrusted input. Do not depend on the model obeying instructions, an MCP tool's
description, or an agent client's confirmation dialog.

The protected deployment assumes the agent has no root/sudo authority, access to
the protected service account, control of the container runtime, permission to
edit deployed broker code/configuration, or administrative access to the server.
The host kernel, runtime administrators, broker code, and cluster administrators
remain trusted. Host/root compromise, kernel exploits, and physical attacks are
outside this guarantee.

Under those assumptions the service must:

- Keep broker-held SSH keys, reusable authenticated connections, database
  credentials, and internal service credentials inaccessible to the agent.
- Enforce project membership, approval state, resource policy, and budget
  reservations before every broker submission, irrespective of client.
- Submit the specific reviewed package and preserve an audit trail of decisions.
- Prevent one project from reading another project's broker-stored artifacts or
  operating its jobs through the API.

These guarantees have explicit boundaries:

| Boundary | What is and is not guaranteed |
| --- | --- |
| Agent vs broker on the host | A separate service identity protects broker files and sockets. A container started under an agent-controlled account does not provide this guarantee. |
| Broker vs arbitrary cluster code | Job code runs on the cluster, never inside a component holding broker credentials. A compromised job may still attack its cluster environment. |
| Project budget | Reservations govern all submissions through this broker. They do not govern direct submissions, other SSH credentials, or jobs spawned from a job outside the broker. |
| Approval | Approval identifies exact package bytes and requested resources. Mutable dependencies, input data, and same-UID interference are separate runtime risks. |
| Multiple projects on one remote Unix UID | API authorization is enforced, but Unix permissions do not isolate those projects' remote processes and files. This is not a hostile multi-tenant compute boundary. |
| Human vs desktop agent | A browser session controlled by the agent cannot establish that an approval was made by a human. Strict human approval requires an independent trusted approval channel. |

Do not advertise an absolute account-wide compute cap without cluster-side
enforcement. Arbitrary jobs may call `sbatch`/`srun`, modify user-owned startup
files, alter `authorized_keys` where permitted, or access data belonging to the
same remote UID. A private broker SSH key prevents disclosure of that key; it
does not make a normal remote user account a sandbox. Existing personal keys
that remain in an agent-readable home directory are also outside broker control.

For stronger enforcement, a later administrator-assisted profile needs isolated
remote identities/workspaces plus cluster submission and network restrictions.
Slurm provides a cluster-side job-submit plugin interface, but deploying a
plugin requires cluster cooperation and a reviewed design; it is not required
for the initial broker. [Slurm job-submit plugin API](https://slurm.schedmd.com/job_submit_plugins.html)

## 2. Deployment profiles

### Protected personal deployment: initial supported security profile

An administrator installs the release as a dedicated Linux service identity.
The agent's normal desktop account must not be able to switch to that identity
or control its runtime. A one-time privileged installation is compatible with
unprivileged daily use; claiming same-device isolation without that setup is not.

Proposed host layout:

| Location | Owner and purpose |
| --- | --- |
| `/opt/compute-broker/` | Administrator-owned, immutable deployed release and deployment manifests; no source bind mount from the agent workspace. |
| `/etc/compute-broker/` | Protected configuration; only administrators can change cluster endpoints, images, trusted keys, and administrative policy. |
| `/var/lib/compute-broker/` | Service-owned database and content-addressed artifact store; parent directories deny agent traversal. |
| `/var/lib/compute-broker/connectors/<connection-id>/` | Connector-private credentials, pinned host keys, and connection metadata; mode `0700`, secret files `0600`. |
| `/run/compute-broker/` | Private runtime sockets, ephemeral authentication state, and SSH control sockets; no sockets exposed to the desktop account. |

The installed service must not load modules, templates, plugins, executables,
Python environments, SSH configuration, or startup scripts from the agent's
writable repository/home directory. A protected secret mounted into
agent-editable executable code is not protected.

Docker Compose is a packaging option for the API, policy worker, and connector.
Run containers as non-root, drop unnecessary capabilities, set
`no-new-privileges`, use a read-only root filesystem and bounded temporary
storage, and mount only each component's required volumes. Never use privileged
containers, host PID namespaces, the Docker socket, or the desktop home mount.
Keep database and connector management endpoints on private networks.

The administrator may manage a rootful daemon, or a dedicated service account
may own a rootless daemon. Neither daemon's control socket is accessible to the
agent. Docker documents that daemon control can allow unrestricted host
filesystem access. [Docker Engine security](https://docs.docker.com/engine/security/)

Bind the personal API to loopback by default, but still require authentication
and authorization: other local programs can reach loopback. Apply Host/Origin
validation, CSRF protection for browser state changes, request size limits, and
rate limits. The agent receives a project token; there is no public local admin
endpoint or unauthenticated setup route after initialization.

### Shared server deployment

The implementation contract is [the shared-service specification](hosted-architecture.md)
and [ADR 0005](adr/0005-hosted-identity-and-connectors.md). These are pending
runtime work, not properties established by the current development portal.

Use the same domain logic with PostgreSQL, HTTPS, organizational identity
authentication, project roles, and per-user cluster connections. Organizational
login identifies a user; the broker separately authorizes project membership,
approval, budgets, and use of each connection. An LDAP group must not implicitly
grant use of another user's SSH identity.

Support explicitly selected local or LDAP login, with stable provider/subject
mapping and no LDAP-to-local fallback. LDAP-only users have unusable local
passwords. Bounded directory freshness and revocation checks apply to human
sessions, issuing users' project keys and dispatch, not only the login screen.

Do not centrally store user SSH private keys in a database, encrypted vault or
runner home. Control has no user SSH-agent/control-socket mounts. A server
connector uses a separately isolated, expiring authenticated session only after
a concrete keyless activation flow passes site acceptance. Treat its socket as
an access capability and acknowledge privileged host compromise. Otherwise use
an outbound personal connector. Default/shared-UID containers do not isolate
unrelated users' active SSH sessions.

A protected personal connector may remain on a user's device. It initiates an
authenticated outbound connection to the server, has its own revocable identity,
and is paired to specific cluster connections. Central policy and budget state
remain authoritative. An offline connector cannot consume a cached balance or
accept a new submission. Already submitted Slurm jobs continue independently.

Connections belonging to unrelated remote users should use separate connector
containers/process identities and credentials. The initial single-owner
connector profile must not be presented as hostile multi-user isolation merely
because a server UI has been added.

### Development deployment

A same-user checkout can run the fake backend without elevated installation.
Mark it clearly as a development profile. It must use synthetic keys and fake
clusters in CI. Do not claim broker secret isolation for real credentials in
this profile. Enabling a real connector requires the protected deployment
checklist, rather than silently inheriting a developer's SSH agent.

## 3. Component authority and internal protocol

Use separate authorities even if personal deployment packages them together:

| Component | Allowed authority |
| --- | --- |
| MCP/API adapter | Authenticate requests and call domain operations; no SSH keys, arbitrary shell tool, direct Slurm gateway token, or unrestricted database interface. |
| Django control service and orchestration worker | Authorize users, hold the authoritative ledger, create immutable packages, approve transitions, reserve usage, and issue execution authorizations. |
| Cluster connector / Slurm SSH worker | Hold only its assigned cluster credentials; stage approved bytes, perform fixed scheduler operations, collect status/usage/artifacts. Poll a narrow control API and report observations; no control-database access, human approval, or budget administration. |
| Agent runtime | Upload packages, request execution, inspect authorized jobs/results, and cancel permitted jobs using project-scoped credentials. |
| Human/admin channel | Manage identities, connection enrollment, allowances, key rotation, and approval decisions according to role. |

The connector accepts a versioned structured protocol, not shell strings from
the MCP interface. Each submission authorization binds job ID, attempt ID,
project, connection, immutable connection-configuration digest, package digest,
effective resource/charge envelope, policy snapshot, reservation ID, expiry, and
a unique dispatch nonce. Changes to the configured host, remote identity,
working root, trusted binaries, host-key trust, or allocation model require a new
connection revision and invalidate packages for the old target. Authenticate
internal traffic with mTLS or
an equivalent protected local channel. Initial workers use authenticated online
authorization through the narrow control API. Short-lived signed dispatch
documents can additionally protect grants relayed through separately
administered infrastructure; do not put a signing key in an agent process or
invent an offline-spending mechanism around those documents.

The connector verifies the authorization and package hash, records the nonce
durably, and obtains current authorization immediately before dispatch. A
replayed dispatch returns its known result or reconciliation state, not a new
job. A generic upload-and-execute endpoint, arbitrary SSH option, or unrestricted
file-transfer endpoint would bypass the security design and is prohibited.

Database/outbox transitions and reservation checks are transactional. Network
delivery is at least once; the protocol must not claim exactly-once Slurm
submission. After an ambiguous SSH failure, retain the reservation and reconcile
the attempt before any retry. Policy edits, connection revocation, or token
revocation affect undispatched work; record the bounded race once dispatch has
crossed the point of no return. Cancelling an accepted cluster job is a separate
operation, not an assumed effect of revoking a token.

## 4. Credential classes and lifecycle

Agent tokens are intentionally available to agents. Generate at least 256 bits
of random secret material, display once, and store only an identifier and a
one-way verifier. Bind permissions server-side to project, subject, permitted
connections, expiration, and scopes such as `jobs:submit`, `jobs:read`,
`jobs:cancel`, and `artifacts:read`. Multiple tokens share the same project
allowance. A key cannot raise its allowance, approve its own job, enroll a
connector, select an arbitrary remote identity, or grant broader scopes.

Privileged secrets include SSH private keys/passphrases, connector enrollment
credentials, mTLS private keys, database passwords, signing keys, and external
identity-provider client secrets. Store them under protected service ownership,
or in an administrator-managed secret store with narrowly scoped retrieval.
Secrets must not enter Git, image layers, build arguments, project artifacts,
URLs, command-line arguments, exception messages, or telemetry.

Compose grants secret files to selected services but delivers file-backed
secrets as bind mounts. Therefore host ownership and access remain essential;
the Compose declaration is not encryption at rest. [Compose secret handling](https://docs.docker.com/compose/how-tos/use-secrets/)

Bootstrap through a protected administrator terminal or a separate trusted admin
device. Generate fresh broker keys within the protected identity and register
their public keys through the normal cluster process. Importing a key that was
already agent-readable does not make its past copies disappear. There are no
default admin passwords. A one-time enrollment secret is short-lived, consumed
atomically, shown only to the trusted administrator, and never emitted in
general service logs.

Rotation must support overlap and explicit revocation for project and connector
tokens. Replacing a cluster key requires closing old multiplexed sessions and
coordinating removal of the old authorized public key. Protect backups like live
secrets and test restoration. Encryption with a decryption key on the same host
can help protect exported backups, but does not defeat a process already running
as the protected identity. Disable unnecessary core dumps and diagnostic
environment dumps in secret-bearing components.

## 5. SSH authentication and university MFA

Initial transport uses an administrator-managed OpenSSH client and protected
configuration. Connection enrollment fixes the host, port, remote user,
credential reference, known-hosts fingerprint/CA, allowed Slurm accounts, and
working root. Never consume the agent's `~/.ssh/config`; it can contain commands
and other executable configuration.

Use explicit trusted executables/configuration, pinned host keys with strict
checking, disabled agent/X11 forwarding, no arbitrary port forwards, a sanitized
environment, and bounded connect/command timeouts. Remote command arguments are
validated and safely encoded for the remote shell; invoking the local SSH
process with an argument list does not remove remote-shell injection risks.
OpenSSH documents connection sharing and executable configuration directives.
[OpenSSH client configuration](https://man.openbsd.org/ssh_config)

Per-connection authentication state machine:

```text
disconnected -> authenticating -> ready
                       |            |
                       v            v
                 auth_required <- MFA session expiry/loss
                       |
                       +-> trusted human reconnect -> authenticating

Key-only connection loss: ready -> disconnected
Any non-revoked state -> disabled -> disconnected (administrator re-enables)
Any non-revoked state -> revoked (permanent)
```

Internal-cluster key authentication can reconnect automatically if its policy
allows it. For the university, use a trusted, narrow authentication helper to
start the SSH master inside the protected connector identity and complete the
cluster's interactive challenge. The helper exposes only the authentication
exchange for a configured connection, never a shell or a reusable SSH socket.
Use a private per-connection control-socket directory; treat that socket as a
credential. Ordinary worker calls must not prompt or repeatedly trigger MFA.
If the MFA master is gone, fail closed to `auth_required` and keep existing job
state/reservations until reconciliation. `disabled` is a reversible
administrative pause; `revoked` permanently invalidates that connection identity.
Session expiry is neither administrative revocation nor proof a job ended.

Do not store MFA answers or forward them to a model, agent logs, project files,
or a reusable prompt transcript. SSH-agent sockets must stay private as well:
OpenSSH explicitly warns that another process under the same Unix user can
abuse them. [OpenSSH authentication-agent documentation](https://man.openbsd.org/ssh-agent)

Connection sharing is a verified OpenSSH capability; acceptance by this
university's Cisco-backed authentication policy, lifetime, and concurrency
behavior are unverified. Test them with administrators. If the site does not
permit reuse, record an unsupported authentication capability and require a
different approved connection method. Do not automate around the second factor.

For strict protection from a desktop-controlling agent, perform enrollment and
MFA interaction through an independent trusted session/device. A desktop agent
may observe or act in a terminal/browser available to it even though it cannot
read the protected key file.

## 6. File staging, job code, environments, and results

Uploads become immutable content-addressed objects after validation. Version one
accepts individually uploaded regular files and rejects archive imports. Package
filenames are relative POSIX paths with bounded depth/length; reject absolute
paths, traversal, NULs, control characters, duplicate/ambiguous names, symlinks,
hard links, devices, FIFOs, and oversized/decompression-bomb inputs. Never use
unrestricted archive extraction in any later importer. Local writes use
descriptor-relative operations without symlink traversal and restrictive
permissions.

Wire integers are bounded by `2^53 - 1`; checked BIGINT database arithmetic is a
separate implementation concern. Reject duplicate JSON keys, nonfinite numbers,
invalid UTF-8 and unpaired Unicode surrogates before schema validation or
hashing. Current planning-contract checks exercise these parser rules; they do
not constitute a runtime parser, RFC 8785 canonicalization, or verified package
integrity. Runtime preparation must use a vetted canonicalization library and
cross-language test vectors.

The connector stages only the sealed manifest into a generated per-attempt
remote directory beneath its configured root. Stage to a temporary location,
verify hashes, then finalize. Do not concatenate agent filenames into a shell
command, follow remote symlinks, accept arbitrary download destinations, or
perform privileged recursive deletion using agent-provided paths.

The generated batch script is submitted as fixed bytes, with the manifest,
environment specification, and resource request recorded for review. Slurm
transfers the batch script to its controller but does not stage the rest of the
user's files; file transfer is the broker's responsibility.
[Slurm batch submission](https://slurm.schedmd.com/sbatch.html)

Jobs under the same remote UID can race or replace staged inputs and interfere
with other jobs. Hash checking before submission and at job start detects some
changes but is not strong isolation against that UID. The approval UI must
distinguish sealed uploaded files from references to mutable cluster data,
modules, package repositories, and images. Pin software where possible; never
claim a hash of a script pins an unversioned `pip install` or remote URL.

No uploaded Python, shell, notebook, dependency installer, environment hook,
deserializer, or image build runs inside the API, policy worker, or credential
connector. Environment creation runs as an authorized scheduled job, or in a
separate build sandbox with no broker secrets and an explicit budget. Do not
give a package installer a connector's Docker socket. Remote login shell startup
files and output are untrusted: OpenSSH executes remote commands through the
user's shell, so assuming that a fixed command makes a compromised remote home
safe would be incorrect. [OpenSSH server execution behavior](https://man.openbsd.org/sshd)

Artifact collection copies only permitted bounded outputs into quarantine, then
the protected object store; do not execute, import, unpickle, or render active
content there. Use bounded/paginated log reads and strip terminal controls in
previews. Serve HTML/SVG and other active content as attachments or through a
separate sandboxed origin. Do not embed raw logs in executable markup. Apply
project authorization to every artifact request and use short-lived scoped
download references where necessary. Treat retrieved text as untrusted input to
the model; output text cannot authorize a new job or alter broker policy.

The connector never forwards its SSH agent, internal tokens, project token,
cloud metadata credentials, or service network access into a job environment.
Cluster jobs inherit the remote identity's access to data and network according
to cluster policy; the initial broker cannot impose a universal remote network
sandbox. Project-scoped private package credentials need a separate future
design and are not silently copied into arbitrary jobs.

## 7. Approval and budget authorization

The approval record binds package digest, resource request, target connection and
its immutable configuration digest, environment definition, policy version,
effective allocation and charge envelope, maximum reservation, reviewer
identity, time, and decision comment. A changed script, file, environment request,
account, connection configuration, or resource specification creates a new
review version. Revalidate current
membership, connection ownership, policy, and available budget at dispatch even
when an earlier package was approved. Rejection comments are data, never
instructions that bypass these checks.

Separate API scopes for human approval/administration from project agent keys.
Check CSRF, session expiry, object-level authorization, and replay protection on
approval and allowance changes. For automatic execution, the project's signed-in
administrator sets the finite allowance and expiry through that privileged
channel; agents can consume it through ordinary submission operations.

Same-browser confirmation, re-entering a password visible to the desktop agent,
or hiding an admin token elsewhere in that user's home is not an independent
human boundary. The protected profile must document whether it provides
ordinary convenience approval or strict approval from a separate device/session.
The strict profile fails closed if that channel is unavailable.

Budget reservations are shared across all keys and subagents in a project.
Requested Slurm time limits are whole minutes in version one. A prepared
`maximum_charge_duration_seconds` additionally includes a verified finite margin
for applicable `KillWait`, `OverTimeLimit`, and termination/cleanup behavior.
Reserve maximum allocated cores/devices multiplied by that charge duration,
rather than multiplying only the requested scheduler walltime. A connection
without a verified finite model cannot admit automatic spending. Slurm documents
the time-limit behaviors that make this distinction necessary.
[Slurm batch time limits](https://slurm.schedmd.com/sbatch.html)

Evaluate the effective prepared envelope after site resource rounding against
the automatic-execution envelope and hard project/installation caps. A small
request that causes a larger allocation is not an exemption. Mixed-mode review
can authorize exceeding the automatic envelope, but cannot bypass hard caps or
available project funds. Charge duration and requested scheduler time retain
their distinct policy meanings and are both visible to the reviewer.

Reserve atomically before submission, retain the reservation while
queued/running/uncertain, and settle once authoritative accounting is available.
Unknown accounting, altered resource allocations, automatic requeues,
and duplicate/ambiguous submissions cannot silently release allowance. Backend
capabilities determine supported limits; reject automatic execution for resource
forms whose conservative maximum cannot be calculated. Approval does not itself
make an unbounded job charge safe.

An append-only application audit stream records actor, project, action, package
digest, reservation, connector/remote identity, decisions, attempts, and
reconciliation outcomes. Do not log secret material or raw MFA prompts/answers.
An application audit log is not tamper-proof against its database administrator;
an external append-only destination can be added for institutional requirements.

## 8. Security validation and release gates

Security tests belong in the implementation's normal test suite, with privileged
host tests explicitly opt-in and synthetic credentials only:

1. Run an adversarial unprivileged host account: it cannot read service secrets,
   database, runtime sockets, Compose files containing privileged configuration,
   deployed executable code, container runtime socket, or protected `/proc`
   details. It cannot replace a parent directory or restart services with its
   own configuration. A separate harmless canary secret verifies isolation.
2. Exercise every agent scope against cross-project jobs/artifacts, approvals,
   connection enrollment, admin endpoints, policy updates, and allowance edits;
   all denied paths have assertions and audit entries.
3. Fuzz manifests, archive rejection, filenames, Slurm fields, remote command
   encoding, malicious symlinks, result HTML, terminal escapes, and giant logs.
   Malicious inputs must not escape storage roots or execute locally.
4. Use a controllable SSH test server for pinned-host-key failures, authentication
   failure, socket expiration, fake keyboard-interactive challenges, malicious
   remote output, connection races, and ambiguous submission acknowledgments.
5. Mutate a package or connection-configuration revision after approval and
   replay/expire/revoke internal dispatches; assert no new execution. Confirm a
   remote connector cannot approve jobs, increase its budget, or submit from an
   offline cached balance. Distinguish MFA expiry, administrative disabling and
   permanent connection revocation.
6. Race budgeted requests from several project tokens and restart workers during
   every dispatch transition. Assert the ledger never authorizes overspend and
   uncertain attempts retain their reservations. Include whole-minute rejection,
   finite termination margins, policy checks after resource rounding, integer
   precision boundaries, and unknown site models.
7. Scan release artifacts, image layers, logs, repository history, and exported
   configuration for synthetic secret canaries; never use real keys in tests.
8. Validate the production Compose/system-service profile in CI or a release VM:
   no source bind mounts, privileged containers, public internal ports, unsafe
   runtime sockets, or excess secret mounts. Test backup restoration and token
   rotation without making old tokens valid again.

Before calling a release suitable for real protected execution, publish a
security support policy, private vulnerability-reporting channel, supported
versions, dependency/image pinning strategy, and a reproducible threat-model
test report. Public examples contain fictional hosts and synthetic data.

## 9. Decisions that need site validation

- Does university policy allow a protected persistent SSH connection, and how is
  Cisco authentication presented to a helper without exposing a general shell?
- Which separate trusted channel will be used for strict human approval and
  protected credential enrollment on a desktop-controlled device?
- Are different projects allowed to share a remote UID, with the documented
  limitations, or does the site require isolated execution identities?
- Can each cluster expose reliable allocated resources, elapsed time, and
  requeue history for conservative budget settlement? What operator restrictions
  prevent already accepted jobs from being enlarged or requeued outside policy?
- Which host operating systems receive a tested protected installer first?
  Linux is the initial target; Docker Desktop packaging alone is not a verified
  equivalent isolation profile on another operating system.
