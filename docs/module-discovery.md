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
`/etc/profile`, the supported module initialization files, and Modules/Lmod
`modulespath` or `.modulespath` files. It extracts literal `MODULEPATH` and
related path settings as data. It does not source a shell profile, evaluate
variables or substitutions, run module commands, or read module-file contents.
Literal site path data takes precedence over generic installation defaults.

Lmod documents that sites can define `MODULEPATH` before initialization or use
its `.modulespath` file, and that the latter can contain directory wildcards.
The broker supports one final `/*` component with bounded directory iteration;
it does not run a general shell or recursive glob.
[Lmod installation and path configuration](https://lmod.readthedocs.io/en/latest/030_installing.html).

Discovered aliases resolve to canonical directory roots before the existing
metadata scanner opens them without following links. The same restrictions on
root directories, descendants and files still apply. The scanner permits
non-root site ownership for name-only catalogs, retains its narrow warned
exceptions for shared system ancestors, and skips unsafe permissions, links,
special files and housekeeping names. Personal home directories are not
searched automatically. A valid system initializer may be reported for recipe
prefill, but it is not executed during this inventory.

The snapshot includes `requested_inventory_mode=auto`, `inventory_mode=metadata`,
`provisional=true`, canonical `module_paths`, `path_provenance`, bounded
`configuration_sources`, and partial/truncated warnings. `available` contains
candidate module names for the existing selector. `loaded=[]` together with
`loaded_state=not_observed` explicitly means loaded state was not measured.
Unsupported dynamic configuration is reported rather than executed; an
operator can supply explicit paths if needed. The catalogue remains subject
to the existing 512-name, 4096-entry, 256-directory and time bounds.

For example, the lab's literal system setting names
`/opt/apps/modules/Core`, `Software`, `Tools`, `Suits` and its Lmod utility
directory. Those aliases resolve to the shared site installation. A personal
connector can discover the scientific Python versions from these paths
without evaluating site-admin-owned module code.

The other modes retain their meaning:

- `metadata` requires explicit canonical `module_paths` and reads filenames
  only. It never reads initialization contents or executes module commands.
- `availability` remains the default for existing protected worker profiles.
  It audits root ownership and write protection before calling the fixed
  `module --terse avail` probe. It does not silently evaluate a non-root tree
  or become an automatic metadata inventory.

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
