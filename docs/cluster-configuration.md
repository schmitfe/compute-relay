# Cluster configuration defaults and owner review

After your personal connector finishes discovery, open the cluster overview and
select **Configure cluster**. Browse remote directories through the connected
worker, choose a baseline working directory, review discovered resource defaults
and confirm the resource ceilings. Saving queues a human-owned setup
request; the worker validates the path, persists its new scope and acknowledges
the configuration before new jobs are allowed. No remote files are created by
selection or validation. Existing execution and accounting must finish before
the configuration can change. Project keys cannot access these setup routes.

The directory browser also offers **New folder** inside a writable parent. It
creates one private directory and opens it for selection. This explicit action
is separate from selecting or validating a baseline; it never recursively
creates parents, overwrites a file or follows a child symbolic link. A retry
opens an already existing ordinary directory without altering it. Choose
**Use this working directory** and save the configuration to adopt the folder.

Invalid configuration fields display their messages directly below the input.
The summary at the top links each message to its field, including the baseline
directory. No worker configuration request is queued until validation succeeds.
Project settings list unfinished clusters beneath **Allowed clusters**, with a
link to the remaining setup step. They cannot be granted access until setup is
complete; saved discovery alone does not confirm their accounting assumptions.

Under **Software discovery**, the owner may enable **Also run module avail during
discovery**. After the worker confirms this setting, run discovery again. The
fixed module listing runs under the connected HPC account with bounded time and
output; it can evaluate the site's module configuration, without accepting an
agent-supplied command. It complements the provisional filename inventory.
The choice is saved for this cluster and in its local worker state. An older
worker must restart before it can acknowledge this option. It defaults off and
can be disabled through the same form.

Under **Execution confirmation**, the owner of a personal cluster can select
**Allow automatic execution on this cluster**. It defaults off for new clusters.
Unchecked, the cluster requires a human decision even when its project is set to
**Automatic within limits** and its API key follows that policy. Checking it
removes this additional cluster requirement; project review mode, key-specific
confirmation, resource limits, available allowance and normal authorization
checks continue to apply. A managed worker's profile remains operator controlled.

The connector applies this choice through the same owner-authenticated, revision
checked configuration handshake as the other cluster settings. Saving the form
queues the change; it becomes active only after the connector acknowledges the
matching profile and configuration. Opening the page, discovery and changing a
project's mode never enable it. Unchecking the option restores cluster review.

**Cluster configuration** is the human-facing name. The internal configuration
object remains `site_profile`, so existing packages and worker profiles retain
their versioned field names. This guide describes the derivation/form contract;
the calling view and worker remain responsible for authorization, review,
configuration revisions and application of changes.

Saved discovery supplies suggestions, not scheduler authorization. Largest
single-node CPU and memory advertisements provide the resource ceilings, reduced
by known partition CPU/node-memory limits. A known per-CPU memory ceiling is
also considered at the observed node CPU ceiling. Partition and cluster totals
are never summed, and a node appearing in two partitions is never counted as
extra capacity. These meanings follow [Slurm's partition configuration](https://slurm.schedmd.com/scontrol.html#SECTION_PARTITIONS---SPECIFICATIONS-FOR-CREATE-AND-UPDATE-COMMANDS).

GPU-count suggestions conservatively use the largest count of a single GPU
family on one node, rather than adding different families. Explicit type
requests remain supported. If project permission covers all known types,
generic requests can let Slurm choose hardware; this does not raise that ceiling.
Typed counts are listed separately in the evidence. A generic GPU count may be
shown with its type explicitly unknown; shared MPS/shard advertisements do not
become physical GPUs. Duplicate or contradictory GPU advertisements remain
unknown. See [Slurm GRES semantics](https://slurm.schedmd.com/gres.html).

The portal edits maximum scheduler walltime in whole minutes and converts it
to exact seconds only at the saved profile boundary. Partition walltime becomes
a finite whole-minute ceiling. Fractional minutes are
rounded down so the suggestion cannot exceed the observed limit. An explicitly
unlimited scheduler walltime is bounded by the broker's supported maximum.
Missing/malformed time remains unknown. Slurm accepts several duration forms
and schedules walltime in minutes; see [the job time option](https://slurm.schedmd.com/sbatch.html#OPT_time).

The implementation currently caps suggestions and owner inputs at 1,024 CPUs,
16 GPUs, 16,777,216 MiB and seven days per job. The evidence explains
when such a cap applies. These are software bounds, not claims about a site's
physical resources or allowance. Known independent maxima can come from
different nodes or partitions; their combination is not necessarily schedulable.
Node/partition live state is not used as a persistent resource limit.

Account and job-QoS suggestions come only from a successful snapshot of the
remote user's associations. A partition's QoS or `AllowAccounts=ALL` is not an
account grant. Restricted or unavailable association queries leave unknowns;
the owner may supply explicit safe names. Cached association membership does
not establish current eligibility, account/QoS limits or scheduler acceptance.

## Preserving owner choices and safety evidence

Derivation fills missing fields. It preserves existing owner ceilings,
allowlists and allocation rounding; a later discovery does not
silently widen a saved owner restriction. Empty existing allowlists are also
deliberate values. Discovery cannot enable automatic execution: a fresh profile
has `automatic_execution_allowed: false`; an already explicit true setting is
retained rather than inferred again.

No CPU/GPU allocation floor is inferred from hardware capacity. Resource
suggestions do not prove rounding, memory-driven CPU allocation, billing behavior
or requeue safety. Existing package preparation and project compute limits still
apply. Reservations use effective allocated resources multiplied by requested
walltime; final accounting uses observed allocated resource time in full.

**CPU allocation unit (logical CPUs)** is separate from the cluster's retained
minimum allocation. It describes the increment to which allocations round up:
`ceil(max(requested CPUs, minimum allocation) / unit) * unit`. For example, unit
2 turns a request for 3 logical CPUs into an effective allocation of 4. A minimum
allocation of 3 also rounds to 4 with unit 2. The first rounded allocation must
fit within **Maximum CPUs per job**.

Saved scheduler topology may suggest a unit for the selected partitions. The
form labels this as provisional metadata and shows its per-partition evidence;
it is not verification by a compute job. A saved explicit unit takes precedence
over a new suggestion. Unknown evidence leaves the field blank. Owners can enter,
change or explicitly clear the unit; an omitted field from an older form retains
the saved value, or leaves it unspecified if none was saved. A suggestion alone
does not add a unit to an omitted form submission. Saving a displayed unit still
requires the normal connector acknowledgement before it changes new packages.

This setting changes effective CPU allocation and its reservation. It does not
change requested walltime, introduce timing margins or enable automatic
execution. Project limits continue to apply to the rounded allocation.

The resource form requires an explicit finite value when discovery could not
provide one. Values must fit the supported bounds, walltime must be whole
minutes, names must be safe scheduler names, and partition selection cannot be
empty. Where selected partitions have known ceilings, inputs cannot exceed
those ceilings. A lower resource ceiling cannot contradict retained allocation
rounding. The retained CPU/GPU minimum floors are not editable through this form;
the explicit CPU allocation unit is the separate owner setting described above.
Forged minimum-floor fields cannot change them. Automatic execution changes only through the
explicit personal-cluster checkbox described above.

The form has no separate termination, overtime or cleanup section. Actual elapsed
allocation time can exceed requested walltime, and final charges are not capped
at the reservation. Debt prevents new project admissions until resolved.

The automatic-execution checkbox is an owner choice, rather than a conclusion
from saved resource visibility. Configuration review remains distinct from
discovering a working directory or granting a project access.

Agent target discovery, project context and allowance responses include
`approval_reasons` alongside `requires_human_approval`. Reasons identify project,
API-key and cluster requirements separately. Mixed projects also expose
`conditional_approval_reasons` and `conditional_approval_notice` before a concrete
allocation is known. A request beyond that project's envelope then records an
explicit project-envelope review reason. These explanations grant no execution
authority, and automatic project/key choices cannot override a cluster review
requirement. The portal shows the same explanations on the prepared-job page.

## Integration API

`compute_relay.domain.cluster_configuration` uses only the standard library:

```python
evidence = derive_cluster_configuration(snapshot, current_profile)
proposed_profile = evidence["site_profile"]
# Also: discovered_limits, per-partition evidence, unknowns, unknown_reasons and warnings.

validated = validate_cluster_configuration(proposed_profile, require_limits=False)
# require_limits=True (default) requires final finite limits and a partition.
```

Unknown resource fields are absent from `site_profile`, and appear as `None` in
`discovered_limits`. Per-partition evidence includes type counts and warnings.
The helper performs no SSH, file access, database query or submitted-code
execution. `ClusterConfigurationError` reports invalid retained/input values.

The Django-only `compute_relay.control.cluster_configuration` provides:

```python
form = ClusterConfigurationForm(
    request.POST or None, snapshot=snapshot, current_profile=current_profile,
)
if form.is_valid():
    edited_profile = form.site_profile()
```

Templates render `form.resource_fields` with field-specific validation messages.
Resource ceilings and ordinary scheduler settings are the owner's inputs.

The caller must authenticate the owner, check the current connection revision,
reject stale/busy changes and arrange protected-worker validation/application.
The form cannot itself change a connection, bind a worker or approve execution.
Tests cover maxima, heterogeneous GPUs, unknowns, implementation caps, retained
settings, requested-walltime reservations, explicit automatic-execution opt-in,
operator-profile isolation, forged rounding fields and a fresh worker-process import with Django and
controller imports blocked. No real cluster or compute allocation is involved.
