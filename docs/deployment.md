# Local installation and recovery

The supported package runs for one person on one device. `compute-relay`
starts its loopback portal, SQLite database, and saved SSH connectors together.
The former shared-server deployment is archived on
`codex/archive-multi-user-server` at `520effc`; LDAP, PostgreSQL, central worker
hosting, and server database-transfer tools are outside the current runtime.

## Install

Use Python 3.12 or 3.13:

```sh
python -m venv ~/.venvs/compute-relay
. ~/.venvs/compute-relay/bin/activate
python -m pip install git+https://github.com/schmitfe/compute-relay.git
compute-relay
```

The distribution has not been published yet. Until publication, use
`python -m pip install .` from the checkout or install a built wheel instead.
All ordinary runtime dependencies are included. Docker is used only by optional
Slurm acceptance fixtures and is unnecessary for normal operation.

## First launch

The portal listens on `127.0.0.1:8768`, and the launcher opens a browser. The
local account is `local`. At the first terminal password prompt, enter and
confirm a password, or leave it blank to choose passwordless local access.
The choice persists. Passwordless mode still uses browser sessions and CSRF
protection; project bearer keys are never treated as human browser identity.

For an explicitly unattended first launch:

```sh
compute-relay --non-interactive --no-password --no-browser --no-connectors
```

To choose a password instead, create a private mode-0600 file yourself and use
`--owner-password-file PATH`. The package reads it without printing its contents.
It does not generate a plaintext password file. Protect the file and remove it
when you no longer need it. An existing installation reuses its saved identity
and login choice; normal restarts do not overwrite them.

## State and configuration

The installation directory is `$XDG_DATA_HOME/compute-relay`, falling back to
`~/.local/share/compute-relay`. It contains the database, artifact store, private
service configuration, and installation-specific connector registry. Use a
private local filesystem with sufficient capacity. A different directory is a
separate installation, not an automatic migration of an existing server.

```sh
compute-relay --data-directory /path/to/private/broker-data --port 8770
```

The portal remains loopback-only. Start options include:

| Option | Purpose |
| --- | --- |
| `--data-directory PATH` | Select durable state for this installation |
| `--port N` | Select the loopback portal port; default 8768 |
| `--extensions LIST` | Select bundled modules as a comma-separated list; an empty list selects core only |
| `--registry-directory PATH` | Explicitly select a connector registry instead of this installation's registry |
| `--no-browser` | Start without opening a browser |
| `--no-connectors` | Run the portal without automatically starting saved connectors |
| `--non-interactive` | Refuse setup or login steps requiring terminal input |
| `--owner-password-file PATH` | Set the local password from a private file |
| `--no-password` | Explicitly choose passwordless local access |
| `--verbose` | Show additional sanitized operational activity |

The port is saved on first setup. Later starts reuse it because MCP origins and
connector bindings depend on that address; use a separate data directory for a
separate installation on another port. Module selection is saved too. Disable a
module only after checking its outstanding work and retained state requirements.

The storage, environments, and workflows modules are bundled and enabled by the
normal launcher. Their project policy and scopes still control use. The lower
level controller and worker entrypoints are developer interfaces; normal users
do not need separate processes or environment-variable configuration.

## Cluster sessions

Create the cluster in the portal and start its connector. SSH login, host-key
confirmation, key passphrases, and MFA are handled by OpenSSH in the original
launcher terminal. The portal can request a start; it does not collect or send
passwords or MFA answers. Prompts are serialized so two clusters cannot ask for
input at once.

Saved connectors belong to the selected installation. Its launcher does not
implicitly start unrelated profiles from the former global registry. Explicit
registry selection remains an advanced connector command. Sessions expire after
their configured lifetime and need another local login.

## Stop, restart, and back up

Keep the launcher terminal running while jobs need submission, observation, or
collection. Ctrl-C stops local services and closes their SSH masters. It does
not cancel jobs Slurm has already accepted. Restart with the same data directory
to reuse projects, budgets, job history, and saved connector configuration.
Outstanding uncertain submissions reconcile using their durable attempt identity;
a restart must not turn into another `sbatch` call for the same attempt.

For a coordinated backup, stop the launcher first and copy the entire data
directory together with any separately stored connector state. Preserve file
permissions. SSH private keys remain in their original local locations and may
need separate backup. Restore into a stopped installation and reconnect before
admitting new jobs. A stale backup can omit jobs submitted after it was taken;
reconcile those jobs rather than starting two competing copies of the ledger.

This same-account package does not protect files or authenticated SSH sockets
from another process with the same account's access. See [security](security.md)
for the actual boundary and [runtime](runtime.md) for execution limitations.
