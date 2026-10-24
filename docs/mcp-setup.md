# Connect a science project to Codex or Claude Code

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

## Add another client or the shared workflow

For a project initialized previously, explicitly reuse its existing private key:

```bash
compute-relay init-mcp --client claude --reuse-key --with-workflow
compute-relay init-mcp --client both --reuse-key --with-workflow
```

Supply the same `--broker-url` for a remote broker. Reuse never prompts for, imports or replaces a key. An already configured broker server is retained only when it exactly matches the generated connection; conflicting settings are refused. The saved workspace metadata and any existing client using this key must agree on the broker origin. If a workspace moves or its broker address changes, review and update the private connection settings rather than silently reusing mismatched metadata.

`--with-workflow` installs the **compute** skill from one maintained provider-neutral source. Codex discovers it at `.agents/skills/compute/SKILL.md`; Claude Code discovers it at `.claude/skills/compute/SKILL.md`. With both clients selected, the two files are byte-identical copies. The shared skill and managed project guidance link to the local usage guide for the actual helper commands. The skill format and discovery locations follow [Codex skills](https://learn.chatgpt.com/docs/build-skills) and [Claude Code skills](https://code.claude.com/docs/en/skills).

The option also appends a short managed block to the selected clients' project `AGENTS.md` or `CLAUDE.md`. Existing project instructions are preserved, including tracked files, and no global client configuration is edited. Secret-free skills and project guidance can be reviewed and committed; the usage guide remains ignored because its command paths are machine-specific. With explicit `--reuse-key --with-workflow`, only the exact current generated skill, managed block and helper guide can be retained. Customized or different content is refused before prompting or mutation; initialization does not migrate earlier templates. Surrounding project instructions, client configurations and the saved key are preserved. Repeating an identical reusable setup adds no duplicate guidance.

The workflow asks agents to inspect current project context, choose local/hybrid/cluster placement, measure bounded pilots, account for CPU parallelism and RAM, respect review and budget holds, and distinguish scientific completion from scheduler completion and final accounting. It guides the model; it does not install hooks, auto-approval rules, a placement engine or a local-shell enforcement boundary. Delegates use the same project connection when available; a parent mediates operations when delegated agents lack tools or permissions.

## Keep source and result bytes out of chat

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

## Bundled module discovery

The stdio adapter authenticates its project key, then reads the broker's current
capabilities. It automatically registers the known enabled storage, environment
and workflow tools without optional dependency extras or new setup flags.
Project scopes and policy still govern their use. An explicit `BROKER_EXTENSIONS`
setting in the client process narrows that advertised set; an empty value selects
core-only tools. A failed or malformed capability response stops startup with a
bounded stderr message rather than silently dropping tools. Reconnect the MCP
adapter after changing the installation's module selection.
