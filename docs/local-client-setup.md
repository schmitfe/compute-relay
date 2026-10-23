# Local project and connector setup

These commands run on the user's device with the broker package installed.
They do not start the broker web service, create a project or issue an API key;
create the project and key in the portal first. Activate the broker's Python
environment before using the commands so the generated MCP configuration uses
that installed interpreter.

## Initialize a science project

Enter the science project directory and run:

```sh
compute-broker init-mcp
```

Paste the project API key at the hidden terminal prompt. The default broker is
`http://127.0.0.1:8768`. To use a different broker or directory:

```sh
compute-broker init-mcp --directory /path/to/science-project \
  --broker-url https://broker.example.org
```

The command creates `.codex/config.toml` and the private
`.compute-broker/project.key`, and adds ignore rules for the local configuration
and private directory to the project's `.gitignore`. It preserves unrelated
configuration and refuses conflicting existing broker configuration or keys.
The key is never printed or passed as a command-line argument. Initialization
does not contact the broker; the MCP adapter verifies the key and resolves its
project when the client connects. The broker retains only the key's hash.

Start a fresh Codex chat in that project and enable/trust its MCP configuration.
The `get_project_context`, `list_compute_targets` and `get_project_allowance`
tools let the agent inspect the project before preparing any work. No project
UUID or credential contents belong in the prompt. This setup configures the
local stdio adapter; HTTP MCP and OAuth remain separate future work.

## Save connector shortcuts

After pairing a connector through its cluster's downloaded setup, save a name
for its permanent state directory:

```sh
compute-broker connectors add lab --state-directory /absolute/private/lab-state
compute-broker connectors add university --state-directory /absolute/private/university-state
compute-broker connectors list
```

The registry stores names and directory paths only. It does not copy SSH keys,
passwords, MFA codes, worker tokens or setup files. It lives in the user's
configuration directory, separate from science project repositories. The
existing state directory remains authoritative for pairing and recovery.

Start one saved connector with:

```sh
compute-broker connectors start lab
```

Or use `compute-broker connectors start` to select a saved name interactively.
The connector runs in that terminal and labels the target before OpenSSH asks
for a key passphrase or MFA response. To start all saved connectors in one
terminal:

```sh
compute-broker connectors start --all
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
compute-broker connectors start lab --verbose
```

The original command also accepts `--verbose`:

```sh
compute-broker connector --state-directory /absolute/private/lab-state --verbose
```

Idle polling stays quiet. Activity messages describe operations without printing
credentials, private-key paths, raw commands, job scripts or workload output.
Removing a shortcut with `compute-broker connectors remove lab` removes only
that registry entry. It does not stop a running connector, revoke its broker
identity, delete local state or cancel jobs.

Keep connectors running while jobs need dispatch, observation or collection.
Their SSH/MFA prompts remain local and interactive. See [personal connectors](personal-connectors.md)
for session expiry, reconnection, revocation and safe recovery.
