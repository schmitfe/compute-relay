# Security boundaries

Compute Relay scopes broker-mediated work with project keys, exact review and a
transactional ledger. It runs under one local OS account and cannot hide files,
SSH sockets or browser authority from a process controlling that account.
Other local users are protected by private permissions; the owner/administrator
and hostile jobs sharing a remote Unix UID are not isolated.

## Local human authority

The portal is loopback-only. Password mode stores a hash. Passwordless mode
exchanges a random single-use launch link for a browser session, redirects away
from the token and retains only its instance-bound hash for five minutes. A new
launch issues a new link. Reaching loopback alone or holding a project key never
grants human authority. Browser mutations require session/CSRF and exact origin/
host checks.

Treat the launch link as a password. Browser launch arguments can reveal it;
cookies span ports on one host. Tokens/sessions are bearer credentials, not UID
bindings. Do not expose the portal through a public listener, proxy or tunnel.
Separate-UID deployments protect credentials only when service state, launch
links and human browser/session are inaccessible to the agent. Never pass an
owner token to an agent instead of a restricted project key.

## Credentials and connectors

Project keys are revealed once, stored hashed and restricted by scopes/policy.
SSH keys stay in local files; OpenSSH prompts for passphrases/MFA in the owner's
terminal. The private authenticated master socket is itself an access capability.
Reviewed connections use explicit identities, pinned known-hosts files and no
agent/X11 forwarding. A dead master cannot silently trigger a new helper login.
Owner-permitted noninteractive renewal is a separate [connector action](personal-connectors.md#resume-and-reconnect).

For a first unknown host, verify its fingerprint independently with the cluster
administrator, or prepopulate the chosen `known_hosts`. A setup download does
not establish host trust. Never trust a fingerprint solely because SSH displays it.
Docker administration can inspect container credentials; fixtures do not add
same-account isolation.

## Workload approval and budget

A project key cannot approve jobs, issue owner keys or raise limits. Approval
binds one immutable execution request; new content/repeats need new authorization.
Project/key policy cannot override a cluster's default-off automatic-execution
gate. Admission, dispatch and accounting use the [core lifecycle](architecture.md).
Lost submit replies retain intent and reconcile identity; cancellation acceptance
alone cannot refund usage. Unknown/mismatched results and superseded generations
must not manufacture completion or free capacity.

These controls do not stop a payload from modifying same-user files, changing
queued jobs or submitting outside the broker. Cluster-side policy is needed to
bound all activity under a remote UID.

## Files, software and outputs

Control, MCP and credential-bearing workers stage payloads as inert data; only
approved compute jobs execute them. Remote helpers are installed, allowlisted
stdlib code with bounded structured operations, never arbitrary shell RPC.
Management validates current protocol and original journal/manifest/native
identity; stored manifests never supply helper source. Missing local inputs
must not prevent management of existing jobs.

Declared paths, byte bounds and hashes govern transfer. Discovered metadata,
source previews and output remain untrusted. Review renders invisible Unicode
and invalid UTF-8 explicitly; large previews are labeled. Do not put secrets in
scripts, names, comments, logs or bundles.

## Input provenance and object publication

A hash identifies bytes, not read authority. Inputs require an independent upload
grant or an exact currently readable producer artifact, including current
project/connection scopes. Package reads, revisions, preparation, execution and
module definitions recheck this. Another blob/job/idempotent replay cannot
launder inaccessible output. Worker outputs/internal generated files do not create
upload grants; explicitly receiving the same bytes does. Unverified legacy input
requires a new explicit upload/revision or readable artifact.

Publication reserves bytes/object slots, then uses a per-project writer lock and
no-follow immutable content-addressed files. Recovery covers every step after
reservation, including directory creation. Age alone never reclaims an active
writer: expired reservations require that same lock. Verify published bytes before
charging once, abort unpublished uploads, and retain corrupt/unknown state charged
for inspection. Recovery receipts do not grant input authority. Busy/failed rows
back off so they cannot hide later crashed uploads.

Resumable transfers expire after their documented inactivity window; a failed final
checksum aborts the reservation and partial. Cached verification binds file
identity, size, mtime and ctime; replacement/change or process restart requires
rehashing. [File transfer](data-transfer.md) defines transfer limits/recovery;
[offline maintenance](deployment.md#inspect-and-compact-local-metadata) defines
administrative reclamation.

## Local storage capacity

Local byte/object quotas and logical metadata quotas are separate admission
controls. Defaults are in [runtime](runtime.md#local-storage-and-metadata-bounds).
Deduplication cannot evade logical metadata charges. Accounting/recovery remain
available when new admissions are full. Indexes, WAL, journals, backups and other
writers consume additional space; quota/compaction charges are not physical disk
measurements and do not erase history or make retired keys executable again.

## Discovered software and remote storage

Metadata discovery reads names/configuration without evaluating modulefiles.
An owner may separately authorize module availability commands that evaluate
site module/rc code. Login-node results do not attest compute-node compatibility.
Environment installation runs only in ordinary approved, charged compute jobs.
Remote storage plans/free-space readings are advisory, not physical reservations
or protection against unrelated NAS writers. See [reporting policy](../SECURITY.md)
for vulnerability reports.
