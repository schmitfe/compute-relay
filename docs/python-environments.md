# Cluster Python environments

Open **Clusters → your cluster → Environments → Add environment**. Recipes belong
to one cluster connection (including its SSH account), and projects explicitly
grant the recipes they may execute. Registering the same host under another
account therefore gives it its own observations and recipes.

1. Choose **Existing Python**, **Environment Modules / Lmod**, **Conda / Anaconda /
   Miniforge**, **Existing Poetry virtual environment**, or **Existing Python
   virtual environment**.
2. Select exact modules from that cluster's saved catalog, or enter exact names
   manually in load order. Conda, Poetry and venv recipes can retain these module
   loads, for example a Miniforge module or the compiler/Python providing runtime
   libraries. Module initialization stays optional and uses the cluster-provided
   command by default.
3. Keep the personal connector running and press **Inspect selection**. This is
   an explicit human action, with CSRF and current connection-revision checks.
   The updated connector must advertise `personal_environments_v1`; restart it
   after upgrading the package. Merely opening or saving the page does not run
   discovery commands.
4. Select **Detected Python** or **Detected environment** from the returned
   dropdowns. A Conda prefix fills its interpreter, prefix and Conda executable.
   Alternatively choose the manual option and enter existing absolute paths.
   Changing selected modules requires a new inspection before reusing detected
   paths. Saving creates an immutable recipe; grant it in project settings.

Inspection runs a fixed, bounded program through the existing authenticated SSH
session. It uses the remote login shell, loads only the selected modules, resolves
Python/Conda/Poetry paths, and uses executable/file metadata instead of executing
discovered Python interpreters. Where Conda is available it runs
`conda info --envs --json` with plugins/config disabled and an isolated HOME;
bounded metadata from the user's `.conda/environments.txt` also contributes
prefixes. Existing Poetry environments are observed beneath the standard
`~/.cache/pypoetry/virtualenvs` directory; custom Poetry locations can be entered
manually. It does not run Poetry in a project or install software on a login node.

Results carry a timestamp, belong to the requesting human and cluster revision,
and are **login-node observations, not compute-node validation**. A cluster test
or normal scheduled job verifies compute-node availability. Missing paths,
truncated results or unusual site setups can still be configured manually.
Conda activation hooks run later inside the authorized compute job, through
`conda run --prefix`; Poetry recipes use their existing venv interpreter directly.

## Agents requesting a new environment

The bundled optional environment-build extension already exposes typed MCP tools
for this workflow, identically to compatible OpenAI and Claude MCP clients:

1. A human grants the base recipe to the project, enables environment builds in
   its environment-build policy, and grants the agent key `environments:build`
   plus the normal job scopes. Source uploads also need `objects:write`.
2. The agent calls `get_environment_build_policy`. Its response includes the
   approved bootstrap recipe references, cluster connection IDs and configurations,
   supported managers, target Python/platform and resource/storage limits.
3. The agent calls `create_environment_build` with a pinned `venv` recipe using
   an approved system or module-provided Python. It can request a standard-library
   environment or a hash-pinned uploaded-wheel closure.
4. `prepare_environment_build` creates a normal reviewable job package.
   `request_environment_build` follows the project's approval/budget policy and
   consumes its ordinary compute allowance and submission slot. Installation and
   verification run in the scheduled compute job.
5. `get_environment_build` returns status and a ready environment reference after
   settled accounting and receipt verification. The human explicitly permits the
   published environment before subsequent jobs use it.

A venv built from module Python retains the base module names, module paths and
optional initializer when reused. Retain its prefix and base runtime on that
cluster. Full Conda/Poetry installation builders are separate future additions;
their existing environments are selectable today. See [environment builds](environment-builds.md)
for the request schema, source constraints, receipts and storage limitations.

The fixed Conda query follows the [official Conda info interface](https://docs.conda.io/projects/conda/en/stable/commands/info.html). The standard Poetry cache and prefix behavior follow [Poetry configuration](https://python-poetry.org/docs/configuration/).
