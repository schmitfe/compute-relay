# Security boundaries

The current package is a single-user application running under your ordinary
operating-system account. It provides project-scoped agent keys, exact review
pages, a transactional compute ledger and constrained cluster operations. It
does not isolate secrets from another process that can read or control the same
account. Private file permissions protect other local users, not the account
owner, an agent acting as that owner, or an administrator.

## Local human authority

The portal listens on loopback. The local owner can choose a password or
passwordless access during setup. Password mode stores a Django password hash;
plaintext passwords are not retained by the service. Passwordless mode treats
access to the local portal as the owner's authority. Browser sessions, CSRF
checks, exact loopback-origin checks and host restrictions still apply. A
project API key is never a substitute for the human session used to approve
jobs or change configuration.

Do not expose the local portal through a public listener, reverse proxy or
forwarding tunnel and treat it as a shared service. Multi-user membership,
LDAP login and PostgreSQL server deployment were removed from this runtime;
the prior implementation is archived on `codex/archive-multi-user-server` at
`520effc`.

## Credentials and connectors

An MCP client holds only its project credential. Project keys are shown once,
stored as hashes by the broker, and restricted by scopes and project policy.
SSH private keys remain in their original device files. OpenSSH reads them and
asks for passphrases or MFA in the launcher terminal. Neither the portal nor the
agent receives those answers. An authenticated SSH master socket is itself a
sensitive access capability and must remain private.

Connector subprocesses use explicit identities, pinned host-key files, no
agent/X11 forwarding and no arbitrary agent-controlled SSH configuration. The
worker reuses its authenticated session; a dead session cannot silently fall
back to new unattended authentication. Starting two clusters serializes their
terminal prompts. Local shutdown closes managed sessions; it does not imply
that Slurm jobs have stopped.

The launcher and connector share the local account. A stronger separation
between an untrusted agent and cluster credentials requires a separate account,
VM or host and a separately reviewed deployment. Docker administration also
allows inspection of container credentials; the optional test containers are
not a claim of same-account secret isolation.

## Workload approval and budget

Preparation freezes scripts, batch directives, environment wrappers, resources
and connection revisions into a canonical digest. A human decision authorizes
one execution request for exactly that content. Changes require a new revision.
Automatic execution remains bounded by project policy, compute reservations,
active-job limits and a durable lifetime submission cap shared by every key.
The connection must also explicitly opt in with
`automatic_execution_allowed: true`; an absent or false value requires review,
including simulated backends. A project or key cannot override a stricter
connection gate.

The worker obtains online dispatch authorization and journals submission intent.
Lost replies reconcile the same attempt rather than repeating submission.
Cancellation acknowledgement does not release funds until definitive native
accounting permits settlement. Stale or unverifiable resource/accounting facts
stop unsafe admission rather than inventing free capacity.

Cancellation intent remains pending after an unknown transport/scheduler result.
Only explicit acceptance or an identity-verified terminal observation resolves
that intent. An identity mismatch is surfaced instead of cancelling a possibly
reused job ID. Renewable generation-fenced leases and durable terminal report
replay retain accounting through slow collection, dropped replies and restart;
a superseded worker cannot report under an old lease.

These controls govern broker-mediated jobs. A payload running under a remote
Unix account may be able to submit other jobs, mutate same-user files or change
queued jobs. Hard limits covering all such activity require cluster-side policy.
Project folders do not provide operating-system isolation between jobs sharing
a remote account.

## Files, software and outputs

Source files are staged as data. They are not executed by the portal, facade or
credential-bearing connector. A fixed one-shot helper uses bounded structured
operations and scheduler argument vectors, not an arbitrary shell RPC. Approved
code executes in a scheduled job. Artifact transfers enforce declared paths,
size bounds and hashes; output text and discovered files remain untrusted data.

The installed remote code is a fixed, allowlisted set of standard-library
components assembled into separate job and discovery bundles. New submission
approval still binds the exact execution source hash. Compatible management of
an existing attempt uses current reviewed code and verifies its original
manifest hash, embedded journal manifest, exact native identity and derived
directory. Stored manifests and journals never supply executable helper source.
Only declared compatible protocols are accepted; use the stopped-installation
[upgrade check](deployment.md#check-an-upgrade-before-starting-it) before migration.

## Input provenance and object publication

A hash is an identifier, not read authority. Each input is sealed to an explicit
byte upload grant or an exact producer artifact. Artifact-backed inputs require
the current project's artifact-read scope and current access to the producer
connection. These checks also apply to package reads, revisions, preparation,
execution and workflow/environment definitions; a second job cannot launder an
unreadable output through its hash, a duplicate blob or an idempotent replay.
An explicitly received upload of the same bytes creates independent input
authority. Internal generated files and worker-reported outputs do not create
that authority merely because storage deduplicates them.

Existing inputs without trustworthy provenance remain unverified. Re-upload
the bytes explicitly into a new revision, or use a currently readable artifact.
The migration does not infer a historical upload from a blob row. Access to a
previously prepared package does not waive the current input checks for new use.

Publication reserves bytes and an object slot before creating files, then uses
an exclusive per-project writer lock and no-follow, immutable content-addressed
publication. Every post-reservation action, including directory creation, is
covered by recovery. A reservation expires after 15 minutes, but an active
writer holding its lock is never reclaimed solely because time elapsed.
Recovery verifies already-published bytes before charging them once, aborts an
unpublished upload, and leaves corrupt or unidentifiable state charged for
inspection. It never converts an upload-recovery receipt into input authority.
Busy or failed recoveries are deferred for 30 seconds and ordered by recovery
eligibility, so a failing batch cannot indefinitely hide later crashed uploads.

## Local storage capacity

Separate logical metadata quotas bound new jobs, idempotency responses and
extension documents even when blob bytes deduplicate or no job is executed.
Object-byte quotas, count limits and bounded audit/report retention provide
additional controls. Current defaults and the explicit offline maintenance
procedure are documented in [runtime](runtime.md#local-storage-and-metadata-bounds)
and [deployment](deployment.md#inspect-and-compact-local-metadata).

Logical charges are not exact SQLite file accounting. Indexes, WAL, journals,
filesystem allocation, backups and unrelated writers can consume additional
bytes or inodes. Quotas and metadata compaction do not guarantee physical disk
headroom, delete remote files, erase execution history or make old idempotency
keys executable again. Valid worker accounting remains admissible when new
metadata admissions are full.

## Discovered software and remote storage

Module metadata discovery does not execute modulefiles. An owner may separately
enable the fixed availability command, which can evaluate site module/rc code
under the remote account. A login-node observation never proves a recipe works
on compute nodes. Optional environment builds and workflows must enter the
ordinary approval, budget and attempt lifecycle.

Storage limits and observed free space are advisory unless a filesystem itself
enforces quotas. Here this refers to remote project storage planning, separately
from the broker's local admission quotas. It is not protection against unrelated
writers filling a shared NAS. Do not put secrets in scripts, job names, comments, logs or source
bundles; keep real configuration and acceptance evidence in ignored private
state. See [security reporting](../SECURITY.md) for reporting policy.
