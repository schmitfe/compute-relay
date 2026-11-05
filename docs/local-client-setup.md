# Local project and connector setup

Start the local portal and its saved connectors with `compute-relay`. The
project/client commands below run separately in your science workspace; create
the project and API key in the portal first. Use the installed Compute Relay
command or interpreter so the generated MCP configuration selects that installation.

## Initialize a science project

Enter the science project directory and run:

```sh
compute-relay init-mcp
```

The default is Codex. For Claude Code, run `compute-relay init-mcp --client claude`;
for both clients and shared compute guidance, use:

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
configurations and private directory. Each selected client runs the installed
interpreter with `-m compute_relay.mcp`, the broker address and absolute key-file
path. Newly created directories and the stored key are private. Initialization
preserves unrelated settings, MCP servers and Git exclusions, and refuses
conflicting existing broker configuration or keys. Claude JSON replacements are
checked and atomic. The formats follow the official [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
and [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp).
It also writes `.compute-relay/USAGE.md`: a secret-free, ignored guide with
the actual installed Python interpreter and workspace paths, shell-quoted for
direct use even when they contain spaces. Its commands use absolute
`python -m compute_relay.cli` invocations and do not depend on a console script
being on `PATH`. Initialization prints the guide's path. The same interpreter
can initialize a project through `-m compute_relay.cli init-mcp` if needed.
The key is never printed or passed as a command-line argument. Initialization
does not contact the broker; the MCP adapter verifies the key and resolves its
project when the client connects. The broker retains only the key's hash.

Private credential/configuration paths already tracked by Git must be untracked
before initialization; ignore rules cannot protect tracked files. Symlinks,
hard-linked setup files, incompatible TOML/JSON and private directories with group
or other access are refused before a key is requested or files are created.
If writing fails, initialization rolls back its own changes where the files
still match what it wrote; concurrent user edits are preserved.

To import an existing key instead of pasting it:

```sh
chmod 600 /path/to/project.key
compute-relay init-mcp --key-file /path/to/project.key
```

Import copies the key into the new project's private directory and preserves
the source. It must be a regular file owned by the current user, without links
or group/other access. Keys cannot be supplied directly as command arguments.

Start a fresh client session in that project and enable/trust its MCP configuration.
`get_project_context` provides the key's permitted compute targets, current limits
and allowance when its scope includes `jobs:read`. Refresh narrower tools as
needed before preparing/admitting work. No project
UUID or credential contents belong in the prompt. This setup configures the
local stdio adapter; HTTP MCP and OAuth are unsupported. See the
[roadmap](roadmap.md) for the conditions to consider hosted support.

`--client claude` selects Claude Code. For Claude Desktop with local stdio
support, manually copy the `compute_relay` entry from generated `.mcp.json` into
its supported application configuration, keeping the same command, arguments
and environment. Initialization does not edit global client settings.

Agents with local Python execution can also use the [Python SDK](python-sdk.md)
with the same initialized workspace and credential. The generated helper guide
includes a bounded script that prepares, requests and waits for a job, then
exports verified results without sending every status response through the model.
Human review and the broker's current limits apply to either interface.

To add the other client or workflow to an already initialized project, explicitly
reuse its key; retain the original `--broker-url` for a remote service:

```sh
compute-relay init-mcp --client both --reuse-key --with-workflow
```

For Claude Code only: `compute-relay init-mcp --client claude --reuse-key --with-workflow`.

Reuse does not prompt for, import or replace the key. Existing client
configuration and saved workspace metadata must agree on the broker origin.
If the workspace moves or its broker address changes, review the private
connection settings before reusing them.

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
To refresh an older guide, preserve custom edits, remove only
`.compute-relay/USAGE.md`, then rerun with `--reuse-key` and the original client/
broker selections. Never remove the connection metadata or key to refresh it.

The skill asks agents to inspect context, choose local/hybrid/cluster placement,
measure bounded pilots, share resource limits and distinguish scientific
completion from execution, accounting and delivery. It guides the agent without
changing broker authority. A parent mediates when delegates lack tools or
permissions. See [investigation and completion guidance](investigations.md).

### Read current context and status

The stdio adapter authenticates the key and discovers enabled modules from
capabilities. No optional dependency extras or additional flags are required.
Project scopes and policy still govern use. An explicit `BROKER_EXTENSIONS` in
the client process narrows that set; an empty value selects core-only tools.
Failed or malformed capability responses stop startup with a bounded error.
Reconnect MCP after changing the installation's module selection.

Job tools return compact summaries by default, including validation, approval,
matching execution/attempt, accounting, result availability and next action.
`get_job(detail=True)` includes the returned page's full metadata; status history
is bounded and paginated as described in [completion](investigations.md#completion-and-local-evidence).
`get_project_allowance()` uses `response_mode: budget`; `detail=True` retains
the fuller allowance metadata. Unknown accounting and scientific validity stay
unknown until their separate checks complete.

`get_project_context.adapter` and MCP startup instructions identify the local
adapter's package/schema versions and source fingerprint captured at startup.
They help identify an old adapter after an upgrade; they do not authenticate
the separate broker's version.

### Keep source and result bytes out of chat

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
the initialized workspace; a remote broker or another host's MCP process does
not acquire access to those files.
