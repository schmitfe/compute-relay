# Portal guide

`compute-relay` opens the local owner portal. Configuration and approval require
a human browser session and CSRF protection; project keys cannot use those forms.
See [security](security.md) for the local-account boundary.

## Add a cluster

1. **Clusters → Add cluster**: enter a name, Slurm login hostname and remote user.
   Advanced settings hold port, local key/known-hosts hints, SSH lifetime and
   optional work directory. Saving does not contact SSH or create remote files.
2. **Start connector**: select the key and answer host-key/passphrase/MFA prompts
   in the launcher's terminal. Multiple clusters' prompts are serialized.
3. After discovery, **Configure cluster**: confirm work directory, resource
   ceilings, scheduler permissions and execution policy. **New folder** is a
   separate explicit action; selecting a directory creates nothing.
4. Save and wait for the worker acknowledgement before granting the cluster and
   its environment recipes to a project.

No manual worker/token registration is needed. **Run discovery** requests fresh
observations; opening pages does not. [Cluster configuration](cluster-configuration.md)
explains resource evidence; [personal connectors](personal-connectors.md) covers
standalone setup and reconnecting.

## Remove a cluster configuration

Owner confirmation archives configuration and blocks new submissions/discovery,
retaining jobs, outputs, accounting and remote files. Normal removal waits for
accounting and collection. **Remove anyway** requires acknowledgement of unfinished
counts and budget holds; it records an audit override without cancelling jobs,
settling unknown usage, freeing funds or resetting limits.

Verify/cancel remaining jobs in Slurm independently. Dedicated personal connectors
are revoked; shared connectors may continue settling existing jobs. Unresolved
historical pages show their last recorded state, which may be stale. Configuration
edits/re-pairing also wait for outstanding execution/recovery to finish.

## Projects and job policy

Set review/automatic/mixed policy, compute allowance, per-job resources, expiry,
active and lifetime limits, clusters and exact environment recipes. Empty access
grants deny execution. Hours become integer ledger seconds; editing totals
preserves spent/reserved usage and cannot reduce below existing commitments.

Preparation holds no compute. Requesting execution reserves the prepared maximum,
including pending reviews; approval transfers that hold without counting twice.
Rejection/cancellation before dispatch releases it. After possible submission,
keep it until definitive accounting. Pending reviews consume no active/lifetime
attempt slot. Each admitted attempt consumes one lifetime slot permanently,
including failures/cancellations; idempotent replay/reconciliation does not.
Default lifetime limit is 1,000; zero blocks admission and exhaustion returns
`TOTAL_JOB_LIMIT` (409).

Project, key and cluster confirmation are independent. Cluster **Allow automatic
execution** defaults off and changes only after the worker acknowledges the
owner's saved revision. Policy changes never execute old pending requests.
Actual allocated usage may exceed the reservation and becomes debt rather than
being truncated; resource/allowance settings are not physical scheduler caps.

GPU checkboxes grant explicit types. None denies GPUs; a partial set requires
an allowed `resources.gpu_type`. Selecting all discovered types may allow
scheduler-chosen hardware only on current fully covered partitions, advertised
as `untyped_gpu_partitions`. Discovery changes never broaden a partial grant;
refresh and save intentionally. Site policy may still demand an explicit type.

## Review and status

Job names use 1–80 letters/digits/underscores/dots/hyphens. Dashboard/project
lists have 50-row pages with All, Unresolved and Awaiting review filters.
Unresolved includes pending reviews, execution, uncertain accounting and unfinished
collection. Live refresh preserves filters, edits and decision comments; it never
admits work. Prepared content is not evidence that execution was requested.

Review exact sources, batch script and environment wrapper. Approve the matching
request or reject with a comment; changed content/resources need a new revision.
Scripts/logs are untrusted text. Native facts and bounded logs help diagnose
failures; historical decisions remain available.

- **Request cancellation** retains uncertain accounting and works at metadata
  capacity. It does not itself confirm native cancellation.
- **Retry output collection** reuses a finished attempt without rerunning compute.
  Disabled new access permits recovery; archived configuration/revoked workers
  cannot. Downloads verify bytes, not scientific validity or acknowledgement.
- **Preview file cleanup** requires separate local/remote confirmations. Save and
  verify wanted results first; [cleanup rules](deployment.md#manual-file-cleanup)
  describe protected data and interrupted deletion.

Allowance, active/lifetime executions, outstanding work, local storage and logical
metadata are separate capacities. Completing accounting/collection frees an
outstanding slot; retained history/files keep their charges. Raising compute or
lifetime limits does not enlarge storage. Current bounds are in [runtime](runtime.md).

Theme choices are **System**, **Light** and **Dark**. System is default; browser
preferences persist where storage is available. Without JavaScript the system
palette applies; without storage a choice works for the current page.

## One-time project keys

Create a named key per client, selecting scopes, expiry, cluster restrictions and
**Follow project policy** or **Always require confirmation**. Issuance/rotation
reveals the secret once; only a hash and independent metadata/ID are retained.
Reload cannot retrieve it. Keys share project allowance/lifetime limits; revocation
stops new access/undispatched work while preserving attribution and accounting.

The key page supplies a secret-free client setup command. Paste the key only into
the hidden prompt, never chat or arguments. [Initialize the science workspace](local-client-setup.md)
there. After rotation, replace only its private `.compute-relay/project.key`,
retain owner-only permissions and restart the client session.

## Software discovery and recipes

Discovery observes existing software; it installs nothing and does not attest
compute-node compatibility. Owners may separately authorize module availability
commands; the [catalog guide](module-discovery.md) explains that trust boundary.
Register and inspect immutable recipes through [cluster environments](environment-builds.md#cluster-recipes-and-inspection),
then grant the exact cluster-bound revision. Recipe changes never rewrite old
packages. Managed builds, storage planning and workflows have their own policies;
being bundled grants no extra authority.
