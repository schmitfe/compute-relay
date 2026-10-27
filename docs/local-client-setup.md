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
local stdio adapter; HTTP MCP and OAuth remain separate future work.

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
the initialized workspace. See [portable MCP setup](mcp-setup.md) for safeguards,
key reuse, client formats and the separate manual Claude Desktop configuration.

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
