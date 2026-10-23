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

Each current job selects one GPU type. Its suggestion therefore uses the largest
count of a single GPU family on one node, rather than adding different families.
Typed counts are listed separately in the evidence. A generic GPU count may be
shown with its type explicitly unknown; shared MPS/shard advertisements do not
become physical GPUs. Duplicate or contradictory GPU advertisements remain
unknown. See [Slurm GRES semantics](https://slurm.schedmd.com/gres.html).

Partition walltime becomes a finite whole-minute ceiling. Fractional minutes are
rounded down so the suggestion cannot exceed the observed limit. An explicitly
unlimited scheduler walltime is bounded by the broker's supported maximum.
Missing/malformed time remains unknown. Slurm accepts several duration forms
and schedules walltime in minutes; see [the job time option](https://slurm.schedmd.com/sbatch.html#OPT_time).

The implementation currently caps suggestions and owner inputs at 1,024 CPUs,
16 GPUs of one type, 16,777,216 MiB and seven days per job. The evidence explains
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

The resource form requires an explicit finite value when discovery could not
provide one. Values must fit the supported bounds, walltime must be whole
minutes, names must be safe scheduler names, and partition selection cannot be
empty. Where selected partitions have known ceilings, inputs cannot exceed
those ceilings. A lower resource ceiling cannot contradict retained allocation
rounding. Allocation-rounding and automatic-execution fields are not editable
through this form; forged extra fields cannot change them.

The form has no separate termination, overtime or cleanup section. Legacy timing
properties do not affect new reservations or active agent limits. Actual elapsed
allocation time can exceed requested walltime, and final charges are not capped
at the reservation. Debt prevents new project admissions until resolved.

An owner must not turn saved visibility into unattended spending without the
separate site evidence and project authorization. Configuration review is also
distinct from discovering a working directory or granting a project access.

## Integration API

`compute_broker.domain.cluster_configuration` uses only the standard library:

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

The Django-only `compute_broker.control.cluster_configuration` provides:

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
settings, requested-walltime reservations, forged automatic/rounding fields and a fresh worker-process import with Django and
controller imports blocked. No real cluster or compute allocation is involved.
