# Module catalog discovery

A login-node catalog is an observation, not loaded shell state or proof that a
recipe works on compute nodes. Personal connectors use:

```json
{"environment_discovery": {"module_inventory_mode": "auto"}}
```

## Metadata modes

`auto` reads bounded, root-owned, non-writable system configuration at known
locations (`/etc/profile`, profile snippets, Modules/Lmod path/initializer files).
It extracts literal `MODULEPATH`, `module use` and Tcl `prepend-path MODULEPATH`
data, following only protected literal configuration references. It never sources
profiles, evaluates variables/substitutions or reads/evaluates modulefile contents.
Literal site paths take precedence over generic defaults.

Conditions form a `literal_candidate_union`, not the final interactive shell path.
Comments are ignored; `module unuse` and dynamic configuration remain unsupported
and disclosed. Only one final `/*` wildcard is supported, with bounded iteration.
Canonical roots are scanned descriptor-relatively without following descendants'
links, special files or housekeeping entries; homes are not searched automatically.

Root-declared paths may contain software-group-writable metadata trees, labeled
`root_declared_group_managed_metadata`. This name-only exception never permits
world-writable trees or module evaluation. Explicit `metadata` mode instead needs
canonical `module_paths` and keeps its stricter write policy. Existing protected
profiles default to `availability`, which audits root ownership/write protection
before a fixed `module --terse avail`; it never silently evaluates a non-root tree.

Initializer metadata distinguishes observed aliases from canonical targets.
`initializer_candidates` retains paths, source, UID/GID/mode, trust and
`verified_on_compute=false`. A protected Lmod `init/profile` link may suggest its
protected sibling `init/bash`; a site-owned target is merely
`site_metadata_candidate`. Selected `init_path`/`canonical_init_path`,
`observed_init_path` and verification flags preserve that distinction. Login-local
initializers may not exist on compute. Initializers remain optional human-reviewed
recipe inputs; discovery never changes historical recipes or mandates one.

Snapshots expose mode/provenance, `provisional=true`, canonical paths, bounded
configuration sources and partial/truncated warnings. `available` lists candidate
names; `loaded=[]` with `loaded_state=not_observed` means unmeasured, not empty.

| Bound | Automatic | Explicit metadata |
| --- | --- | --- |
| Configuration | 64 files, 128 KiB, 64 roots, 2 seconds; bounded profile listing | Explicit paths |
| Catalog | 8,192 names, 32,768 entries, 4,096 directories, depth 12, 20 seconds | 512 names, 4,096 entries, 256 directories, depth 8, 2 seconds |

Limits always mark incomplete results. Paths appearing only through module code
need explicit operator paths or separately authorized availability. Auto mode
neither runs `spider` nor evaluates its Lua cache.

## Optional availability command

```json
{"environment_discovery": {
  "module_inventory_mode": "auto",
  "module_availability_enabled": true
}}
```

This owner option defaults false and is valid only with auto mode. It separately
authorizes evaluating site module/rc code under the remote account, including
software-group-managed code reached by the module system. A fixed noninteractive
Bash command sources the separately audited root-owned `availability_init_path`,
sets discovered `MODULEPATH`, and runs `module --terse avail`. Metadata initializer
candidates cannot replace that trusted command initializer or authorize execution.
No personal startup, input, arbitrary command, installer or job is requested.

The command is bounded to eight seconds, 256 KiB per stream and 8,192 names.
`metadata_available` retains filename results; `availability` records status,
names, return code, truncation and `module_code_evaluation_authorized=true`.
`available` is their bounded union. Failure/truncation preserves metadata and
marks partial results. No `module load`/`spider` is requested, though initialization
itself may load defaults. Availability does not attest compute compatibility.

All intended roots are independently validated before recursive enumeration.
Snapshots separate intended, validated and reached `module_paths`; availability
uses the full validated list even if metadata scanning exhausts its budget.
Path validation has a separate 20-second bound: incomplete validation fails
closed with `full_modulepath_validation_incomplete`, never a shortened search path.

Changing a personal profile changes its fingerprint. The connector must advertise
it and the portal request fresh discovery. Changed connection bindings require
new preparation; running jobs are not moved/reconfigured.

References: [Lmod paths](https://lmod.readthedocs.io/en/latest/030_installing.html),
[spider](https://lmod.readthedocs.io/en/latest/136_spider.html),
[cache](https://lmod.readthedocs.io/en/latest/130_spider_cache.html),
[Modules initialization](https://modules.readthedocs.io/en/latest/design/initial-environment.html)
and [rc files](https://modules.readthedocs.io/en/latest/user-guide.html).
