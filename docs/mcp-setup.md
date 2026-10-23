# Connect a new Codex project

Create a project API key in the broker portal, then run this command from the science project's directory using the Python environment containing the broker:

```bash
compute-broker init-mcp
```

Paste the key at the hidden terminal prompt. The command uses `http://127.0.0.1:8768` by default. A project UUID is unnecessary: the API key identifies its project and permissions when the MCP adapter connects.

For a broker hosted elsewhere or another destination directory:

```bash
compute-broker init-mcp --directory /path/to/science-project --broker-url https://broker.example.org
```

Initialization creates the destination if needed, writes `.compute-broker/project.key` with owner-only access, and adds a `compute_broker` server to `.codex/config.toml`. The configuration contains the currently active Python interpreter's absolute path, the broker address, and the key-file path. Key contents are kept out of the configuration and command arguments. Newly created directories are private. The generated `.gitignore` entries exclude the private broker directory, key, and local MCP configuration.

Unrelated existing Codex settings and Git exclusions are preserved. Existing broker keys or server configurations are refused rather than overwritten. Files already tracked by Git must be explicitly untracked before initialization; ignore rules cannot protect tracked files. Symlinks, hard-linked setup files, incompatible TOML, and private directories with group or other access are refused.

If the key is already stored in a private file, import it instead of pasting it:

```bash
chmod 600 /path/to/project.key
compute-broker init-mcp --key-file /path/to/project.key
```

Import copies the key into the new project's private directory and preserves the source. The source must be a regular file owned by the current user, without symlinks, hard links, or group/other access. API keys cannot be supplied directly as command arguments. Initialization is offline and does not create or validate a project on the server.

Start a new trusted Codex chat in the initialized project. Ask the chat to inspect the `compute_broker` tools, permitted clusters, and available allowance before submitting work. The broker and the relevant personal cluster connector must be running for remote execution. This command configures the local stdio adapter; the portal address remains its control API address.
