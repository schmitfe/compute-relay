# Portal and protected worker configuration

This describes the development alpha. Sign in with the synthetic owner created by `bootstrap-demo` to explore Projects and jobs and Clusters. Each cluster contains its own Environments section. Agent bearer keys cannot access these human management forms. Browser changes require a human session, CSRF protection and the appropriate project or resource ownership. An agent controlling the owner's browser can still operate that session; a protected installation needs a trusted approval channel.

The shipped migrations upgrade existing development databases. Stop the control process, run `python -m django migrate --settings=compute_broker.control.settings` with its existing `BROKER_DATA_DIR`, and restart control and worker. Preserve the data directory and Django secret so history and sessions remain valid. The synthetic bootstrap applies migrations automatically.

| Implemented human route | Purpose |
| --- | --- |
| `/projects/new/` and `/projects/{id}/` | Create/edit project policy; inspect balances, job counts, jobs and keys |
| `/projects/{id}/keys/new/` | Issue and reveal a new key once |
| `/projects/{id}/keys/{key}/edit/` | Edit metadata, scopes, confirmation and cluster restrictions |
| `/projects/{id}/keys/{key}/rotate/` and `/revoke/` | POST-only rotation or revocation |
| `/clusters/`, `/clusters/new/`, `/clusters/{id}/` | Owned cluster inventory and enrollment |
| `/clusters/setup/{id}/`, `/edit/`, `/bind/`, `/worker-config/` | Saved cluster setup, later worker/profile binding, and an inert configuration template |
| `/workers/new/`, `/workers/{id}/`, `/workers/{id}/remove/` | Human worker registration, safe metadata and owner-confirmed removal |
| `/clusters/{id}/discover/` and `/toggle/` | POST-only discovery refresh or admission enable/disable |
| `/clusters/{id}/environments/` and `/clusters/{id}/environments/new/` | List/create immutable recipes for that cluster |

Worker profile advertisements use `/internal/v1/worker/profiles`; leased discovery reports use `/internal/v1/connections/{id}/report`. These require worker credentials and do not accept project bearer keys. The general REST administration endpoints described in the target API document are not implemented substitutes for these human forms.

**Remove worker** is available in the Clusters worker list and on a worker's
detail page. Removal revokes its broker credential, disables its cluster
connections, and invalidates unfinished pairing/setup operations. Any execution
with unsettled accounting blocks removal, including terminal jobs still awaiting
final usage. Jobs, artifacts, ledger entries and saved cluster configurations
remain available. To reuse a saved personal cluster, explicitly pair a replacement
connector, run discovery and confirm its configuration again. Removing a worker
does not delete its local state directory or any SSH key.

## Projects and jobs

Create a project and select its execution policy, total CPU core/GPU device hours, resource bounds, expiration, simultaneous jobs, lifetime total submission limit, allowed clusters and allowed environments. Empty cluster/environment selections deny execution. Hours become integer seconds in the authoritative ledger. Editing totals preserves spent and reserved compute and cannot reduce a total below existing commitments.

The total submission limit counts durable execution authorizations across the entire project. One admitted attempt consumes one slot permanently, including failures and cancellations. Each further execution of the same package needs a new slot. Preparation, revisions, rejected review requests, idempotent replay and reconciliation of the same attempt do not consume slots. All API keys share the count; adding or rotating a key cannot reset it. The cap is checked in the admission transaction, including human approval. Exhaustion returns `TOTAL_JOB_LIMIT` (409). A zero limit blocks the first execution; pause a project to stop admissions after it has history. The portal rejects a total below the existing admitted count. Policies created before this field use a finite default of 1,000.

The allowance API includes `jobs: {limit, admitted, remaining, active, active_limit}` alongside compute balances. Simultaneous and lifetime limits serve separate purposes. Request-rate and draft metadata limits are future work.

Agents supply `request.name`, a 1–80 character name using letters, digits, underscores, dots or hyphens. This becomes the displayed name and Slurm job name. Jobs show their project, owning user, submitting API key, latest execution state and creation date. The owner is the project owner, not an assertion that a human personally submitted the job. Every project has its own job table; the dashboard shows the latest 100 accessible jobs. Preparing an immutable package does not mean execution is still pending: the portal derives Completed, Rejected and other states from the latest execution request/attempt.

The dashboard, project, job review and cluster pages refresh their status sections
automatically every four seconds while visible. New review requests appear without
reloading the page. Review comments and settings being edited stay in place; if
another reviewer decides a request, its buttons become disabled and any unsent
comment remains available to copy. Cluster status refreshes use saved broker data;
discovery and catalog refresh still require their explicit actions. Polling pauses
in hidden tabs, backs off during connection problems, and stops when access is
withdrawn. The read-only `/ui/live/` routes require a current human session and
recheck project roles on every request. They never contact a scheduler or worker.

## One-time API keys

Create separate named keys for each client. Select scopes, expiry, cluster access and execution confirmation. Cluster access either inherits the project's clusters or restricts them to a subset; selecting an empty restricted subset denies every cluster.

| Key setting | Behavior |
| --- | --- |
| Follow project policy | Uses the project's approval/budgeted/mixed policy |
| Always require human confirmation | Every execution waits for a human, including in a budgeted project |
| Automatic within project policy | Automatically admits only when project policy allows it; never overrides project-required review |

All choices retain project compute, resource, expiry and submission limits. Mode edits affect new admission and dispatch: changing an automatic key to require review blocks an admitted attempt that has not obtained dispatch permission. It does not withdraw a permit already granted or cancel an already submitted Slurm job. Existing observations/accounting continue after access changes.

Issuance and rotation show the full random secret once in the successful response. The database retains only a SHA-256 hash plus metadata and an independent UUID identifier; no substring of the secret is used as its display identifier. Audit, session and one-time submission records do not store the value. Responses prohibit caching. Reload/replay does not retrieve it. If lost, rotate or create another key. Rotation atomically revokes the old key and preserves permissions, expiry, execution confirmation and cluster restrictions. Revocation/delete access retains a tombstone so historical jobs remain attributable.

The synthetic bootstrap explicitly exports `agent-token` and `worker-token` as client fixture files for disposable testing. `demo.json` contains the synthetic owner sign-in, not the project token. This test-only exporter is separate from ordinary key management. Do not mount fixture owner/worker files into an agent client or use this same-account setup to protect real credentials.

## Enroll a cluster

1. In **Clusters → Add cluster**, save a name, backend and login hostname. The remote username and proposed job directory may be filled in later. You can propose a new canonical directory outside every currently advertised profile. Saving this record creates no executable connection, SSH request, directory or discovery task.
2. The saved setup page offers **Register a new worker** or a later binding to an existing one. Worker registration issues a worker API token once, with only its hash retained by control. Store it in that worker's private token file; this token is separate from project keys and SSH credentials. Legacy workers with no owner still need trusted operator assignment.
3. Once a remote username and proposed directory are saved, download the worker configuration template. On a new worker, replace the private-key and pinned known-hosts path placeholders. For an existing worker, merge only the new credential profile into its private configuration, preserving other profiles and settings. The template approves exactly the proposed target/root and starts with automatic execution disabled; verify interpreter paths, site resource bounds and timing assumptions separately. The portal does not install/start a worker or save SSH credentials.
4. Start/restart the worker. It advertises safe aliases, exact allowed targets and directory prefixes. Invalid profiles are omitted independently. Private identity files, known-hosts files, MFA socket paths, executable paths and discovery search roots remain on the worker. No local path mapping is inferred from the browser's filesystem.
5. On the saved cluster, choose **Connect worker**, select an owned worker, load its current profiles, and choose an approved remote job directory. If a new directory is not approved yet, keep the setup saved while updating the worker's protected mapping. Binding rechecks the saved revision, current worker ownership/revocation, profile fingerprint, login target and directory under transaction locks.
6. Binding creates a normal connection and queues discovery. Project execution stays unavailable until first discovery succeeds. Failed discovery or an enable toggle cannot bypass this setup gate. **Run discovery** requests a fresh asynchronous query; **Reload saved discovery** reads only the saved result. Grant the identified cluster and its environment recipes to projects afterward, optionally narrowing access per key.

Saved registrations appear under **Setup in progress** and can be edited until
bound. Once bound, they point to the ordinary cluster overview. Target edits do
not rewrite an existing connection or any job's immutable execution revision;
create another setup for another binding. Existing connections remain available
after this additive database upgrade. Validated one-step enrollment POSTs remain
supported for older trusted operator clients.

The resource table reports saved advertised CPU/memory/GRES capacities and its
timestamp. It omits changing partition/node states; historical states remain in
the saved JSON. A directory mapping grants the worker a staging scope, not a
general permission for an agent to copy or delete arbitrary remote files.

Disabling a cluster blocks new access without abandoning submitted jobs' accounting. Resource advertisements do not establish permissions or guarantee immediate capacity. Allocation rounding and usable final accounting must be verified before unattended spending. Reservations use requested walltime, and actual observed allocated runtime is charged in full.

Set protected `site_profile.automatic_execution_allowed` to `false` while a site is undergoing supervised acceptance. This requires a fresh human approval even if the project or key permits automation; the browser cannot override it. Omitted flags preserve the earlier operator-configured behavior. A trusted operator enables automation only after checking allocation/accounting semantics. A requested-walltime reservation does not guarantee a physical ceiling on elapsed usage; debt from final settlement blocks new admissions.

Use [the protected profile example](../examples/worker-profiles.json) as a structural starting point. Every hostname, path, partition, account and resource bound is illustrative and must be replaced/verified. Any legacy timing properties are ignored for new reservations. Protect this file and deployed code with the service identity. Configure pinned host keys; do not disable checking. An MFA profile can reuse a separately established protected SSH ControlMaster, but the institutional interactive login/reconnect flow remains an acceptance task.

Keep aliases and helper releases immutable until all affected attempts finish accounting. Use a new alias and connection for changed execution settings while old jobs run. Unchanged inventory snapshots may refresh during execution. A changed connection revision is rejected while attempts remain unsettled. After settlement, authorized discovery can renew derived profile bindings; previously prepared packages then require new revisions. Legacy UUID configurations can continue existing jobs but need profile enrollment for browser discovery.

## Discover software and create recipes

Software discovery reports login-node metadata and marks it unverified on compute nodes. It checks common system Modules/Lmod initialization and module trees automatically, subject to bounded ownership/permission checks. Sites with custom trees can set `environment_discovery.module_init` and `module_paths`. Empty or rejected paths are reported explicitly; operators may need to configure narrower trusted roots. Startup/profile files and personal module hooks are excluded from discovery.

The default module inventory mode, `availability`, runs the module availability command only after auditing root-owned module trees. Sites with software directories maintained by another service account can instead set `environment_discovery.module_inventory_mode` to `metadata` and provide explicit, resolved `module_paths`. This mode lists regular module-file names using bounded directory metadata; it never sources initialization scripts or evaluates module files. Symlinks and writable module roots/entries are excluded. Root-owned shared ancestors with sticky world-write or group-write permissions can be traversed in this mode with explicit warnings; this does not establish trust for evaluating their contents. These names are provisional candidates: the catalog does not resolve aliases, dependencies, dynamic hierarchy visibility or module compatibility.

Conda is queried only through an explicitly configured executable, with plugins disabled and a scrubbed environment. Existing Python, venv, Conda and Poetry candidates come from configured paths/search roots, bounded by depth and count. The broker never executes candidate Python interpreters or Poetry projects and does not scan a whole home directory. Configure dedicated environment roots rather than arbitrary project trees. Absence from the catalog means not discovered, not necessarily unavailable.

In **Clusters → select a cluster → Environments**, create an immutable recipe. The cluster is fixed by the page. For Modules/Lmod recipes, choose exact versions from its searchable saved catalog, or enter exact names in the advanced field. Selected entries load first, with manual entries appended in their entered order. Use only the advanced field when a particular load order is required.

Projects display recipe permissions grouped by their allowed clusters. New jobs require the exact recipe ID, revision and cluster binding; a recipe from another cluster cannot be substituted even if both have the same interpreter or recipe digest. The upgrade conservatively binds unambiguous historical recipes. Ambiguous legacy recipes remain unassigned and cannot authorize new execution; recreate them for their intended cluster. Historical package reads, observations and accounting remain available.

| Kind | Reviewed job behavior |
| --- | --- |
| Existing Python/Bash | Execute the selected existing absolute interpreter |
| Modules/Lmod | Use the cluster-provided module command in a login batch shell, set optional approved `MODULEPATH`, purge and load exact modules, then run the chosen interpreter |
| Python virtual environment | Use its existing interpreter directly; set `VIRTUAL_ENV` and `PATH` without sourcing activation hooks |
| Conda | Run the explicitly selected Conda executable/prefix inside the approved compute job; activation hooks can run there |
| Poetry virtual environment | Use the existing venv interpreter directly; no Poetry CLI or project hooks in the broker/login worker |

Discovered module paths and environment locations prefill suggestions only for an owned cluster. A human chooses a recipe and grants it to projects. Edits require a new recipe/revision, so reviewed packages retain their original binding. These recipes reference existing mutable installations; their digest is not an attestation of every installed package. The optional, default-disabled [environment-build extension](environment-builds.md) provides the separately authorized scheduled venv builder.

Module recipes default to **Use the cluster-provided module command**. Their
batch script uses `#!/bin/bash -l`, so the compute node's normal site/account
startup supplies the command. It restores the Slurm submission directory after
startup before opening the package. No explicit Modules/Lmod initialization
file is sourced by the default wrapper. An advanced explicit initializer remains
an optional fallback, used only if the command is missing. If the command is
still unavailable, the wrapper exits with a fixed `MODULE_COMMAND_UNAVAILABLE`
message rather than running the program in the wrong environment. Login startup
runs inside the approved compute job; catalog discovery does not source personal
startup files. Existing reviewed packages remain immutable and need a new job
revision to adopt this batch-shell behavior.
