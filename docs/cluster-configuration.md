# Cluster configuration and resource evidence

After discovery, **Configure cluster** selects a baseline work directory, resource
ceilings, scheduler permissions and execution confirmation. Saving queues a
human-owned revision; the worker validates and acknowledges it before job access.
Project keys cannot call setup routes. Selection/validation creates no remote
files, and binding changes wait for existing execution/accounting/recovery.

**New folder** creates one private child of a writable directory. It never creates
parents, overwrites a file or follows a child link; retry opens an existing ordinary
directory unchanged. **Use this working directory**, then save, to adopt it.
Invalid fields queue no worker request; incomplete clusters cannot be granted to
projects. Saved discovery alone confirms neither authority nor accounting.

**Allow automatic execution on this cluster** defaults off. Only its owner can
change it through the acknowledged configuration handshake; project/key policy
cannot override it. **Also run module avail during discovery** separately permits
the fixed bounded site-code evaluation described in [module discovery](module-discovery.md).
It defaults off; save, wait for acknowledgement, then request new discovery.
Older workers must restart before acknowledging new options.

## Resource derivation

The internal object remains `site_profile`. Suggestions are evidence, never
scheduler authorization or live available capacity:

- CPU/memory ceilings use largest single-node advertisements, narrowed by known
  partition/per-CPU limits. Never sum partitions or double-count overlapping nodes.
- GPUs use the largest count of one family on one node. Unknown types, conflicting
  advertisements and MPS/shards do not become verified physical GPU capacity.
- Walltime is a finite whole-minute ceiling; fractional minutes round down,
  explicitly unlimited values use the software maximum, malformed values stay
  unknown. The portal converts minutes to seconds at the saved-profile boundary.
- Account/QoS suggestions require successful user-association observations.
  Partition QoS or `AllowAccounts=ALL` grants nothing. Restricted queries remain
  unknown and require explicit safe names; cached membership is not eligibility.

Software maxima are 1,024 CPUs, 16 GPUs, 16,777,216 MiB and seven days per job.
Independent maxima may come from different nodes/partitions and need not coexist.
Unknown required limits need explicit owner values. Inputs must use safe names,
nonempty partitions, known partition ceilings and compatible retained allocation
rounding. Discovery fills missing fields only, preserving explicit ceilings,
empty allowlists and review choices; it never enables automatic execution.

## Preserving owner choices and safety evidence

Capacity does not establish allocation rounding, memory-driven CPU allocation,
billing or requeue safety. Existing minimum CPU/GPU floors are retained and not
editable through this form; forged floor fields cannot change them.

**CPU allocation unit** is a separate owner setting:
`ceil(max(requested CPUs, minimum allocation) / unit) * unit`. Unit 2 rounds a
request/minimum of 3 to 4, which must fit the CPU ceiling. Topology suggestions
are provisional, not compute verification. Explicit saved units win; owners may
change/clear them. An omitted field retains a saved unit but never adopts a new
suggestion. Changes require worker acknowledgement and affect effective allocation
and reservation, not requested walltime or execution permission.

Current target/context responses expose `requires_human_approval` and
`approval_reasons`, with project/key/cluster sources. Mixed mode also exposes
conditional reasons before allocation. These explain policy; they grant no
execution authority. Reservation and settlement rules are maintained with the
[contracts](../contracts/README.md).

## Integration API

The standard-library-only `compute_relay.domain.cluster_configuration` provides:

```python
evidence = derive_cluster_configuration(snapshot, current_profile)
proposed_profile = evidence["site_profile"]
# Also: discovered_limits, per-partition evidence, unknowns, unknown_reasons and warnings.

validated = validate_cluster_configuration(proposed_profile, require_limits=False)
# require_limits=True (default) requires final finite limits and a partition.
```

Unknown fields are absent from `site_profile` and `None` in discovered limits.
The helper performs no SSH, file/DB access or code execution;
`ClusterConfigurationError` rejects invalid values. The Django form is separate:

```python
form = ClusterConfigurationForm(
    request.POST or None, snapshot=snapshot, current_profile=current_profile,
)
if form.is_valid():
    edited_profile = form.site_profile()
```

Templates use `form.resource_fields` for field errors. Callers must authenticate
the owner, reject stale/busy revisions and arrange worker validation/application;
the form cannot bind a connection or authorize execution. Unit checks cover
heterogeneous/unknown evidence, retained choices, rounding, forged fields and
worker imports without Django; they do not allocate compute.

Semantics: [Slurm partitions](https://slurm.schedmd.com/scontrol.html#SECTION_PARTITIONS---SPECIFICATIONS-FOR-CREATE-AND-UPDATE-COMMANDS),
[GRES](https://slurm.schedmd.com/gres.html), [walltime](https://slurm.schedmd.com/sbatch.html#OPT_time).
