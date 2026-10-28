# Local portal and cluster configuration

Start the application with `compute-relay`. It opens the loopback portal for the
local owner and runs saved connectors in the same foreground application.
Passwordless access or the saved local password establishes the human session;
a project bearer key cannot access configuration or approval forms. Browser
changes require CSRF protection. See [security](security.md) for the same-account
boundary.

## Add a cluster

1. In **Clusters → Add cluster**, save a name, Slurm login hostname, remote
   username and SSH port. Supply a proposed work directory or choose it after
   login. Saving the setup does not submit a job or create remote files.
2. Select **Start connector**. The local launcher handles the request and asks
   for SSH key selection, host-key confirmation, passphrase or MFA in its
   terminal. The browser does not collect the key or authentication answers.
   Two clusters' prompts are serialized.
3. Wait for login and read-only discovery. Open **Configure cluster** to review
   the resource ceilings, allowed accounts/QoS, work directory, software discovery
   and cluster approval policy. Directory selection itself creates no files;
   **New folder** is a separate explicit action.
4. Save the configuration and wait for the connector's acknowledgement.
   Discovery alone does not authorize spending or confirm the configuration.
   Create the cluster's immutable execution recipes and grant the appropriate
   cluster/recipes to a project.

No manual worker registration, worker-token copy or protected profile editing is
needed. Connector state belongs to this installation and is reused on restart.
**Run discovery** requests another read-only observation; refreshing the saved
page does not contact the scheduler. See [cluster configuration](cluster-configuration.md)
and [personal connectors](personal-connectors.md) for details.

Configuration changes are fenced by saved revisions and cannot replace a
connection with unsettled executions. Disabling or removing a cluster retains
job history, artifacts and accounting. Normal removal waits for final accounting
and output collection. If a connector is no longer usable, the removal page
shows the unfinished execution count and retained budget holds, with a separate
**Remove anyway** action. Acknowledge its consequences before confirming.

Removing anyway archives the configuration and records the override and pending
counts in the audit trail. It leaves execution states, artifacts, accounting and
budget reservations unchanged. It does not stop remote jobs or establish their
final usage; verify or cancel those jobs in Slurm independently. A personal
connector used only by that configuration loses service access. Shared
connectors remain available and can still settle previously submitted jobs.
Historical job pages label unresolved work on a removed cluster as the last
recorded status, rather than suggesting it is a current scheduler observation.
Retained reservations and unresolved attempts can still consume allowance and
active-job capacity; removing a configuration does not reset either limit.

Removal does not delete HPC files or software environments. A stopped or expired
connector needs another local login before it can dispatch or collect results.

## Projects and job policy

Create a project and select its execution policy, CPU-core/GPU-device allowance,
resource envelope, expiry, simultaneous-job limit, lifetime submission limit,
clusters and environments. Empty cluster/environment grants deny execution.
Hours become integer seconds in the ledger. Editing totals preserves spent and
reserved compute and cannot reduce the allowance below existing commitments.

Preparing a package does not reserve compute. Requesting execution does: pending
human reviews hold the prepared maximum resource-seconds, and admitted attempts
retain their reservations. Approval transfers a pending hold without reserving
again. Rejection or **Cancel request and release hold** releases a pending hold.
After dispatch may have started, cancellation retains the claim until definitive
scheduler accounting permits settlement. Pending reviews do not consume active
or lifetime attempt slots and do not expire silently.

Project, key and cluster confirmation settings are independent. Automatic project
policy admits a job only when the key and cluster also permit it. Cluster
**Allow automatic execution** starts disabled and becomes effective only after
the worker acknowledges the reviewed configuration. Changing policy does not
execute older pending requests automatically.

Each admitted attempt consumes one lifetime submission slot permanently,
including failure and cancellation. A new execution or explicit retry needs a
new slot. Preparation, rejected reviews, idempotent replay and reconciliation of
the same attempt do not. Every key shares the count. The cap is checked during
admission and human approval; exhaustion returns `TOTAL_JOB_LIMIT` (409).
A zero limit blocks the first admission. The default total is 1,000.

The allowance API includes `jobs: {limit, admitted, remaining, active,
active_limit}`. Budget and submission counts remain separate controls. Actual
allocated runtime can exceed a request under site scheduling policy; the broker
records observed usage/debt rather than pretending the request was a physical
ceiling. Resource/accounting bounds must be reviewed before unattended spending.

## Review and status

Agent job names are 1–80 characters using letters, digits, underscores, dots and
hyphens. The same name is used for display and Slurm. The project page lists its
jobs, submitting keys, latest execution state and balances. An immutable package
can have a completed or rejected execution; preparation alone does not mean it
is waiting to run.

The review page shows the exact source, batch file and environment wrapper.
Approve the current execution request or reject it with a comment. Source or
resource changes require a new revision and decision. Collected errors, native
exit status and bounded logs help diagnose completed attempts. Logs and scripts
are displayed as untrusted text.

Visible dashboard/project/cluster/review status sections refresh from saved
broker data. Editing a comment or setting is preserved. Discovery and cluster
checks remain explicit actions; read-only status refreshes never submit work.

## One-time project keys

Create a separate named key for each client. Choose scopes, expiry, cluster
restrictions and confirmation mode:

| Mode | Behavior |
| --- | --- |
| Follow project policy | Uses the project's review/automatic policy |
| Always require confirmation | Every new execution waits for human review |
| Automatic within project policy | Automatic only when project and cluster policy also allow it |

Issuance and rotation reveal the complete secret once in a non-cacheable
response. The database retains a hash, metadata and independent key ID. Sessions,
audit events and replay metadata do not retain plaintext. Reloading cannot
retrieve it; rotate or create another key if it is lost. All keys share the
project ledger and lifetime limit. Revocation retains historical attribution and
stops new operations/undispatched work while native accounting continues.

From the science workspace, use `compute-relay init-mcp --client both
--with-workflow` and paste the key at the hidden prompt. The MCP client receives
no SSH credential. See [client setup](local-client-setup.md).

## Software discovery and recipes

Personal connectors use automatic provisional metadata discovery. It extracts
literal paths from protected system configuration and scans bounded module
filename metadata, including root-declared software-group-managed trees.
**Also run module avail during discovery** separately authorizes the fixed module
availability probe. Both results retain their provenance, bounds and omissions;
neither proves a selected environment works on compute nodes. See
[module discovery](module-discovery.md).

Known Python, venv, Conda and Poetry candidates are observations, not installed
or executed by discovery. Dedicated configured searches do not scan an entire
home directory. Missing catalog entries mean undiscovered, not necessarily
unavailable.

In a cluster's **Environments** page, save an immutable cluster-bound recipe.
Choose module names from the searchable catalog or enter exact names in order. Use **Inspect selection** with a running personal connector to populate Python and existing Conda/Poetry/venv dropdowns. Conda and virtual-environment recipes can retain module loads; manual paths remain available. See [Cluster environments](environment-builds.md) for discovery, trust boundaries and agent build requests.
Projects must grant that exact recipe; identical paths on another cluster do not
substitute for its connection binding.

| Kind | Reviewed job behavior |
| --- | --- |
| Existing Python/Bash | Run the selected existing interpreter |
| Modules/Lmod | Use the cluster-provided module command, load selected modules, then run the chosen interpreter |
| Virtual environment | Set its prefix/PATH and run its interpreter without activation hooks |
| Conda | Run the selected executable/prefix inside the compute job |
| Poetry virtual environment | Run the existing venv interpreter; do not run Poetry project hooks in the connector |

Module jobs use a login batch shell and restore the submission directory after
site startup. An explicit initializer is optional advanced configuration, used
as a fallback only if the command is absent. A missing module command produces
`MODULE_COMMAND_UNAVAILABLE` rather than running in the wrong environment.
Recipe edits create a new immutable version; historical packages do not change.

The bundled [environment-build module](environment-builds.md) provides scheduled,
charged reusable builds under explicit project policy. [Storage planning](extensions-storage.md)
and [workflows](workflows.md) likewise require their own scopes/policy even though
normal installation includes them.
