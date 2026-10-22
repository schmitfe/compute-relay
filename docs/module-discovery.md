# Finding the module catalog

Software discovery reports a login-node observation. A module catalog is
different from the modules currently loaded into a shell, and it does not
prove that a recipe works on compute nodes.

Personal connectors use this protected profile setting:

```json
{"environment_discovery": {"module_inventory_mode": "auto"}}
```

Automatic mode builds a **provisional metadata catalog**. It reads only bounded,
root-owned, non-writable system configuration at known locations, including
`/etc/profile`, bounded `/etc/profile.d/*.sh` snippets, supported module
initialization files, and Modules/Lmod `modulespath`, `.modulespath` and
`initrc` configuration. It extracts literal `MODULEPATH` settings and literal
`module use` or Tcl `prepend-path MODULEPATH` declarations as data. Conventional
literal absolute references to module initialization/path configuration can be
followed through the same root-ownership and byte bounds. It does not source a
shell profile, evaluate variables or substitutions, run module commands, or
read software module-file contents.
Literal site path data takes precedence over generic installation defaults.

Shell conditions are not interpreted. The result is a union of literal
candidate paths, including paths in different conditional branches. A
`module unuse` statement is disclosed as unsupported rather than globally
subtracting a directory that might remain active in another branch. Commented
declarations are ignored. The snapshot reports
`path_semantics=literal_candidate_union`; it does not claim to reproduce a
particular interactive shell's final state.

Lmod documents that sites can define `MODULEPATH` before initialization or use
its `.modulespath` file, and that the latter can contain directory wildcards.
The broker supports one final `/*` component with bounded directory iteration;
it does not run a general shell or recursive glob.
[Lmod installation and path configuration](https://lmod.readthedocs.io/en/latest/030_installing.html).

Discovered aliases resolve to canonical directory roots before the existing
metadata scanner opens them without following links. The same restrictions on
root directories, descendants and files still apply. Automatic paths declared
by protected root-owned system configuration additionally permit
software-group-writable trees and ancestors, with an explicit
`root_declared_group_managed_metadata` policy and warnings. This is a name-only
exception: module contents are never read or evaluated, world-writable trees
remain excluded, and directory traversal remains descriptor-relative without
following links. Direct explicit metadata mode retains its stricter write
policy, and the availability execution policy is unchanged. The scanner skips
links, special files and housekeeping names. Personal home directories are not
searched automatically. A valid system initializer may be reported for recipe
prefill, but it is not executed during this inventory.

The snapshot includes `requested_inventory_mode=auto`, `inventory_mode=metadata`,
`provisional=true`, canonical `module_paths`, `path_provenance`, bounded
`configuration_sources`, and partial/truncated warnings. `available` contains
candidate module names for the existing selector. `loaded=[]` together with
`loaded_state=not_observed` explicitly means loaded state was not measured.
Unsupported dynamic configuration is reported rather than executed; an
operator can supply explicit paths if needed. Automatic path discovery has
fixed bounds of 64 configuration files, 128 KiB total configuration data, 64
candidate roots and two seconds; profile directory listing is separately
bounded. Its catalog scanner permits up to 8192 names, 32768 directory entries,
4096 directories, depth 12 and twenty seconds. The longer bounded scan accounts
for cold shared-filesystem metadata caches. Reaching a limit is always
reported as partial/truncated. Explicit metadata mode retains the existing
512-name, 4096-entry, 256-directory, depth-eight and two-second bounds.

For example, the lab's literal system setting names
`/opt/apps/modules/Core`, `Software`, `Tools`, `Suits` and its Lmod utility
directory. Those aliases resolve to the shared site installation. A personal
connector can discover the scientific Python versions from these paths
without evaluating site-admin-owned module code.

The university site illustrates why the generic package defaults are
insufficient. Its distribution `modulespath` contains only system utilities;
the actual software trees are added by root-owned `ramsesmods.sh` and
`zeasybuild.sh` snippets, using literal `module use` declarations and a final
directory wildcard. The scientific trees are maintained by a software group
and are group writable. A read-only acceptance run of the fixed scanner found
2076 candidate names across 1293 directories with no traversal truncation,
compared with five utility names before the fix. The result remains partial
because aliases/symlinks, conditional state and an absent legacy path were
omitted. This measures filename discovery, not compute-node compatibility.

Lmod's normal spider learns additional hierarchy paths by evaluating
modulefiles, and its cache stores Lua data. Automatic metadata mode does not
run spider or evaluate that cache. A compiler/MPI hierarchy whose paths appear
only in module code therefore needs explicit operator-supplied paths or a
separately authorized availability probe; a filename catalog alone cannot
establish prerequisites or correct load ordering.
[Lmod spider behavior](https://lmod.readthedocs.io/en/latest/136_spider.html),
[Lmod system cache](https://lmod.readthedocs.io/en/latest/130_spider_cache.html).

The other modes retain their meaning:

- `metadata` requires explicit canonical `module_paths` and reads filenames
  only. It never reads initialization contents or executes module commands.
- `availability` remains the default for existing protected worker profiles.
  It audits root ownership and write protection before calling the fixed
  `module --terse avail` probe. It does not silently evaluate a non-root tree
  or become an automatic metadata inventory.

An owner can also enable a complementary login-node availability probe:

```json
{"environment_discovery": {
  "module_inventory_mode": "auto",
  "module_availability_enabled": true
}}
```

The option defaults to `false` and is valid only with automatic mode. When
enabled, a fixed noninteractive Bash command sources the known, separately
audited root-owned module initializer, sets `MODULEPATH` to the discovered
catalog roots, and runs `module --terse avail`. It has an eight-second deadline,
256 KiB output limit per stream and 8192-name limit, with no personal shell
startup, interactive input, arbitrary command field, installation or job.
Unlike metadata discovery, this explicitly authorized operation can evaluate
site module/rc code under the SSH user's authority, including software-group
managed code reached by the module system. This is why it requires a distinct
owner-controlled option; it does not relax the existing `availability` mode.

The snapshot retains filename candidates as `metadata_available` and reports
the command result separately under `availability`, including status, names,
return code, truncation and `module_code_evaluation_authorized=true`. The
existing selector's `available` field is the bounded union. Failed or truncated
probes preserve the metadata catalog and report partial results. A successful
availability query establishes neither a loaded environment nor compute-node
compatibility. No `module load`, `module spider` or personal initialization is
requested by the broker.

All intended candidate roots are independently validated before recursive
enumeration. The snapshot distinguishes `intended_module_paths`,
`validated_module_paths`, and `module_paths` actually reached by the metadata
scan. Availability uses the complete independently validated list even when
an earlier recursive tree exhausts the metadata budget. Search-path validation
has its own twenty-second bound; if that bound prevents validating the full
list, availability fails closed with `full_modulepath_validation_incomplete`
instead of silently querying a shortened search path.

The distinction matters because Modules availability commands can evaluate
module rc files, and initialization can itself load defaults. Automatic mode
therefore extracts path data without invoking either.
[Modules initial environment](https://modules.readthedocs.io/en/latest/design/initial-environment.html),
[Modules rc files](https://modules.readthedocs.io/en/latest/user-guide.html).

Changing a personal connector profile to automatic discovery changes its
profile fingerprint. The connector must advertise the new profile and the
portal must request fresh discovery. Existing prepared job revisions retain
their original bindings and must be prepared again if the connection revision
changes. No running job is moved or reconfigured by this catalog update.
