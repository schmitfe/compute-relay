# Local project and connector setup

Start the local portal and its saved connectors with `compute-relay`. The
project/client commands below run separately in your science workspace; create
the project and API key in the portal first. Activate the broker's Python
environment before using the commands so the generated MCP configuration uses
that installed interpreter.

## Initialize a science project

Enter the science project directory and run:

```sh
compute-relay init-mcp
```

The default is Codex. For Claude Code, use `--client claude`; for both clients and
shared compute guidance, use:

```sh
compute-relay init-mcp --client both --with-workflow
```

Paste the project API key at the hidden terminal prompt. The default broker is
`http://127.0.0.1:8768`. To use a different broker or directory:

```sh
compute-relay init-mcp --directory /path/to/science-project \
  --broker-url https://broker.example.org
```

The command creates `.codex/config.toml` for Codex, `.mcp.json` for Claude Code, or
both, sharing one private `.compute-relay/project.key`. Private
`.compute-relay/connection.json` records the broker origin and workspace root for
the file helper, without key contents. Git exclusions cover the selected client
configurations and private directory. Initialization preserves unrelated
configuration and refuses conflicting existing broker configuration or keys.
It also writes `.compute-relay/USAGE.md`: a secret-free, ignored guide with
the actual installed Python interpreter and workspace paths, shell-quoted for
direct use even when they contain spaces. Its commands use absolute
`python -m compute_relay.cli` invocations and do not depend on a console script
being on `PATH`. Initialization prints the guide's path. The same interpreter
can initialize a project through `-m compute_relay.cli init-mcp` if needed.
The key is never printed or passed as a command-line argument. Initialization
does not contact the broker; the MCP adapter verifies the key and resolves its
project when the client connects. The broker retains only the key's hash.

Start a fresh client session in that project and enable/trust its MCP configuration.
`get_project_context` provides the key's permitted compute targets, current limits
and allowance when its scope includes `jobs:read`. Refresh narrower tools as
needed before preparing/admitting work. No project
UUID or credential contents belong in the prompt. This setup configures the
local stdio adapter; HTTP MCP and OAuth are unsupported. See the
[roadmap](roadmap.md) for the conditions to consider hosted support.

To add the other client or workflow to an already initialized project, explicitly
reuse its key; retain the original `--broker-url` for a remote service:

```sh
compute-relay init-mcp --client both --reuse-key --with-workflow
```

The opt-in workflow copies the same provider-neutral skill to Codex's
`.agents/skills/compute/SKILL.md` and Claude Code's
`.claude/skills/compute/SKILL.md`, and appends a small managed block to selected
project `AGENTS.md`/`CLAUDE.md` files. Existing instructions and permission
settings are preserved. No client-wide settings, hooks or automatic approvals
are installed. The secret-free workflow files can be reviewed and committed.
Existing different skills or managed blocks require review instead of overwrite.
Explicit `--reuse-key --with-workflow` can retain exact current generated
skills, managed blocks and the helper guide. Different templates, user edits
and conflicting guides are refused before prompting or writing.

For sources and outputs, copy the complete commands from
`.compute-relay/USAGE.md`, which is linked by the shared skill and project
guidance. The helper keeps chat responses to bounded receipts. Its general syntax
is shown below; the guide supplies the actual interpreter path:

```sh
/absolute/path/to/python -m compute_relay.cli files stage --directory /path/to/science-project --manifest source-bundle.json
/absolute/path/to/python -m compute_relay.cli files prepare --directory /path/to/science-project --bundle BUNDLE_ID --request job.json --idempotency-key RUN_STAGE
/absolute/path/to/python -m compute_relay.cli files fetch --directory /path/to/science-project --attempt-id ATTEMPT_UUID --output results/RUN
```

Large inputs and results use resumable chunks, up to 8 GiB per file. Repeat
`files stage` to resume uploads; add `--resume` to `files fetch` to continue a
matching partial download. `compute-relay logs --attempt-id ATTEMPT_UUID --follow`
follows incremental running-job output, and `compute-relay files retry
--attempt-id ATTEMPT_UUID` retries collection of a finished job's declared
outputs. See [files and live output](data-transfer.md) for retention, quotas and
upgrade details.

Stage only explicit relative paths; the source manifest contains
`{"files":["main.py","src/util.py"]}`. Prepare uses a job request without `files`
or `project_id` and does not execute it. Fetch selects a fresh project output
directory and verifies artifact hashes. Run these commands on the device holding
the initialized workspace. See the **Connect Codex and Claude Code** section
below for safeguards, key reuse, client formats and the manual Claude Desktop
configuration.

## Save connector shortcuts

After pairing a connector through its cluster's downloaded setup, save a name
for its permanent state directory:

```sh
compute-relay connectors add lab --state-directory /absolute/private/lab-state
compute-relay connectors add university --state-directory /absolute/private/university-state
compute-relay connectors list
```

The registry stores names and directory paths only. It does not copy SSH keys,
passwords, MFA codes, worker tokens or setup files. It lives in the user's
configuration directory, separate from science project repositories. The
existing state directory remains authoritative for pairing and recovery.

Start one saved connector with:

```sh
compute-relay connectors start lab
```

Or use `compute-relay connectors start` to select a saved name interactively.
The connector runs in that terminal and labels the target before OpenSSH asks
for a key passphrase or MFA response. To start all saved connectors in one
terminal:

```sh
compute-relay connectors start --all
```

The command signs into the clusters sequentially so their passphrase and MFA
prompts cannot overlap. It then runs their workers together with labeled output.
Ctrl-C closes the group. Individual SSH session deadlines remain in force;
expired or revoked connectors require a new local login. Separate terminals
remain useful when independently restarting just one connector. The
successful-start message means both SSH login and the initial
broker handshake have succeeded; the process then waits for broker work.

For additional sanitized activity messages:

```sh
compute-relay connectors start lab --verbose
```

The original command also accepts `--verbose`:

```sh
compute-relay connector --state-directory /absolute/private/lab-state --verbose
```

Idle polling stays quiet. Activity messages describe operations without printing
credentials, private-key paths, raw commands, job scripts or workload output.
Removing a shortcut with `compute-relay connectors remove lab` removes only
that registry entry. It does not stop a running connector, revoke its broker
identity, delete local state or cancel jobs.

Keep connectors running while jobs need dispatch, observation or collection.
Their SSH/MFA prompts remain local and interactive. See [personal connectors](personal-connectors.md)
for session expiry, reconnection, revocation and safe recovery.

## Connect Codex and Claude Code

Create a project API key in the broker portal, then run this command from the science project's directory using the Python environment containing the broker:

```bash
compute-relay init-mcp
```

Codex remains the default. Select Claude Code or configure both clients with one project key:

```bash
compute-relay init-mcp --client claude
compute-relay init-mcp --client both --with-workflow
```

Paste the key at the hidden terminal prompt. The command uses `http://127.0.0.1:8768` by default. A project UUID is unnecessary: the API key identifies its project and permissions when the MCP adapter connects.

For a broker hosted elsewhere or another destination directory:

```bash
compute-relay init-mcp --directory /path/to/science-project --broker-url https://broker.example.org
```

Initialization creates the destination if needed and writes one `.compute-relay/project.key` with owner-only access. Codex uses `.codex/config.toml`; Claude Code uses project `.mcp.json` with `mcpServers.compute_relay`. Each selected client runs the installed Python interpreter with `-m compute_relay.mcp`, using the same broker address and absolute key-file path. No project UUID or key contents appear in these configurations. `.compute-relay/connection.json` records the broker origin and absolute workspace root for the local file helper; it contains no credential contents. Newly created directories are private. Git exclusions cover the private broker directory and every generated client configuration. These locations follow the clients' project MCP conventions: [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp).

Every initialization also creates **`.compute-relay/USAGE.md`**, a secret-free, ignored guide containing the exact installed interpreter and workspace paths for source staging, preparation and result retrieval. Its commands invoke the absolute Python executable with `-m compute_relay.cli`; they need neither an activated environment nor a `compute-relay` console script on `PATH`. Interpreter and workspace paths are shell-quoted, including spaces or apostrophes. Initialization prints the guide's location. If the console command itself is unavailable when initializing, invoke the installed interpreter directly: `/absolute/path/to/python -m compute_relay.cli init-mcp`.

Unrelated existing client settings, MCP servers and Git exclusions are preserved. Claude JSON replacements are checked and atomic. Existing broker keys or server configurations are refused by default. Files already tracked by Git at private configuration/credential paths must be explicitly untracked before initialization; ignore rules cannot protect tracked files. Symlinks, hard-linked setup files, incompatible TOML/JSON and private directories with group or other access are refused before a key is requested or files are created.

### Add another client or the shared workflow

For a project initialized previously, explicitly reuse its existing private key:

```bash
compute-relay init-mcp --client claude --reuse-key --with-workflow
compute-relay init-mcp --client both --reuse-key --with-workflow
```

Supply the same `--broker-url` for a remote broker. Reuse never prompts for, imports or replaces a key. An already configured broker server is retained only when it exactly matches the generated connection; conflicting settings are refused. The saved workspace metadata and any existing client using this key must agree on the broker origin. If a workspace moves or its broker address changes, review and update the private connection settings rather than silently reusing mismatched metadata.

`--with-workflow` installs the **compute** skill from one maintained provider-neutral source. Codex discovers it at `.agents/skills/compute/SKILL.md`; Claude Code discovers it at `.claude/skills/compute/SKILL.md`. With both clients selected, the two files are byte-identical copies. The shared skill and managed project guidance link to the local usage guide for the actual helper commands. The skill format and discovery locations follow [Codex skills](https://learn.chatgpt.com/docs/build-skills) and [Claude Code skills](https://code.claude.com/docs/en/skills).

The option also appends a short managed block to the selected clients' project `AGENTS.md` or `CLAUDE.md`. Existing project instructions are preserved, including tracked files, and no global client configuration is edited. Secret-free skills and project guidance can be reviewed and committed; the usage guide remains ignored because its command paths are machine-specific. With explicit `--reuse-key --with-workflow`, only the exact current generated skill, managed block and helper guide can be retained. Customized or different content is refused before prompting or mutation; initialization does not migrate earlier templates. Surrounding project instructions, client configurations and the saved key are preserved. Repeating an identical reusable setup adds no duplicate guidance.

The workflow asks agents to inspect current project context, choose local/hybrid/cluster placement, measure bounded pilots, account for CPU parallelism and RAM, respect review and budget holds, and distinguish scientific completion from scheduler completion and final accounting. It guides the model; it does not install hooks, auto-approval rules, a placement engine or a local-shell enforcement boundary. Delegates use the same project connection when available; a parent mediates operations when delegated agents lack tools or permissions.

### Keep source and result bytes out of chat

Read `.compute-relay/USAGE.md` and copy its complete commands. The local workspace helper stages explicit files, prepares requests from a saved bundle and fetches checksummed results. The following is the general syntax; substitute the installed interpreter from that guide:

```bash
/absolute/path/to/python -m compute_relay.cli files stage --directory /path/to/science-project --manifest source-bundle.json
/absolute/path/to/python -m compute_relay.cli files prepare --directory /path/to/science-project --bundle BUNDLE_ID --request job.json --idempotency-key RUN_STAGE
/absolute/path/to/python -m compute_relay.cli files fetch --directory /path/to/science-project --attempt-id ATTEMPT_UUID --output results/RUN
```

`source-bundle.json` contains an explicit list such as `{"files":["main.py","src/util.py"]}`; positional relative source paths are an alternative. No default recursive scan is performed. The job JSON omits `files` and `project_id`; the saved bundle and API key supply them. Receipts contain identifiers, filenames and hashes rather than file bytes. Preparation does not execute the job. Run the helper on the device containing this initialized workspace; a remote broker or another host's MCP process does not acquire access to that device's files. Fetch uses an explicit fresh project subdirectory.

If the key is already stored in a private file, import it instead of pasting it:

```bash
chmod 600 /path/to/project.key
compute-relay init-mcp --key-file /path/to/project.key
```

Import copies the key into the new project's private directory and preserves the source. The source must be a regular file owned by the current user, without symlinks, hard links, or group/other access. API keys cannot be supplied directly as command arguments. Initialization is offline and does not create or validate a project on the server.

Start a new trusted session in the initialized project and enable the client connection using its normal permission controls. Ask it to inspect `get_project_context` before planning work. The broker and the relevant personal cluster connector must be running for remote execution. This command configures local stdio adapters; the portal address remains its control API address.

`--client claude` configures **Claude Code**, not Claude Desktop's global settings. Claude Desktop can use a manually configured stdio server with the same command, arguments and environment from the generated `.mcp.json`; copy the `compute_relay` entry into its supported MCP configuration yourself. This initializer neither edits that global configuration nor registers an HTTP MCP/OAuth endpoint.

### Bundled module discovery

The stdio adapter authenticates its project key, then reads the broker's current
capabilities. It automatically registers the known enabled storage, environment
and workflow tools without optional dependency extras or new setup flags.
Project scopes and policy still govern their use. An explicit `BROKER_EXTENSIONS`
setting in the client process narrows that advertised set; an empty value selects
core-only tools. A failed or malformed capability response stops startup with a
bounded stderr message rather than silently dropping tools. Reconnect the MCP
adapter after changing the installation's module selection.
