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

The worker obtains online dispatch authorization and journals submission intent.
Lost replies reconcile the same attempt rather than repeating submission.
Cancellation acknowledgement does not release funds until definitive native
accounting permits settlement. Stale or unverifiable resource/accounting facts
stop unsafe admission rather than inventing free capacity.

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

Module metadata discovery does not execute modulefiles. An owner may separately
enable the fixed availability command, which can evaluate site module/rc code
under the remote account. A login-node observation never proves a recipe works
on compute nodes. Optional environment builds and workflows must enter the
ordinary approval, budget and attempt lifecycle.

Storage limits and observed free space are advisory unless a filesystem itself
enforces quotas. They are not protection against unrelated writers filling a
shared NAS. Do not put secrets in scripts, job names, comments, logs or source
bundles; keep real configuration and acceptance evidence in ignored private
state. See [security reporting](../SECURITY.md) for reporting policy.
