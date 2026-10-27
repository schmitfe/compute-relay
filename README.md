# Compute Relay

Compute Relay gives Codex, Claude Code, and compatible local MCP clients access to your Slurm clusters through a local portal and a project-scoped MCP interface. Agents prepare jobs, you review the exact script and batch file, and the broker submits and tracks approved work. Projects can instead allow automatic execution within a compute allowance and lifetime job limit.

**Status: development alpha.** One Python package runs on your device. It includes the portal, a persistent SQLite ledger, SSH connectors, the official MCP adapter, software discovery, storage planning, managed environment builds, and artifact-dependent workflows. No separate database server, Docker installation, directory service, or hosted account is required.

Source repository: [schmitfe/compute-relay](https://github.com/schmitfe/compute-relay).

## Install and start

Use Python 3.12 or 3.13 and install the package into a virtual environment:

```sh
python -m venv ~/.venvs/compute-relay
. ~/.venvs/compute-relay/bin/activate
python -m pip install git+https://github.com/schmitfe/compute-relay.git
compute-relay
```

The intended PyPI distribution has not been published yet. Until a release exists, install from this checkout with `python -m pip install .`, or install a locally built wheel. The normal installation includes all runtime dependencies; no extras are required.

The launcher starts the portal at `http://127.0.0.1:8768` and opens your browser. On first launch, choose a local password or leave it blank to use a one-time launch link on this device. The link expires after five minutes; running the launcher again issues a fresh link for an already running portal. Keep the link private. The local account is named `local`. The access choice is saved; subsequent launches reuse the same projects, jobs, budgets, and connector configuration.

Keep the terminal running. Cluster SSH key passphrases and MFA prompts appear there, one connection at a time. Ctrl-C stops the local portal and its connectors; it does not cancel jobs already submitted to Slurm. Run `compute-relay` again to resume monitoring and reconnect.

## Connect a cluster and a project

1. In the portal, add a cluster and start its local connector. Choose the SSH identity on your device and answer any SSH or MFA prompts in the launcher terminal.
2. Review discovered partitions, resource limits, module candidates, and storage paths. Save the cluster configuration and a suitable execution environment.
3. Create a project, select its cluster and environment, set approval and budget limits, and create a project API key. The key is shown once.
4. In your science project directory, initialize your AI clients:

```sh
compute-relay init-mcp --client both --with-workflow
```

Paste the project key at the hidden prompt. The command writes project-local Codex and Claude Code configuration, private key storage, Git exclusions, and a usage guide. It does not put SSH keys or MFA answers into the agent configuration. See [local client setup](docs/local-client-setup.md) for client-specific options and file transfer commands.

## Local state and options

State lives in `$XDG_DATA_HOME/compute-relay`, or `~/.local/share/compute-relay` when that variable is unset. To keep a separate installation:

```sh
compute-relay --data-directory /path/to/private/broker-data --port 8770
```

Useful options include `--no-browser`, `--no-connectors`, `--verbose`, and `--non-interactive`. For unattended first setup, choose `--no-password` explicitly or supply a private password file with `--owner-password-file PATH`. Passwords are never printed or generated into a plaintext file. See [installation and recovery](docs/deployment.md) for backups and restart behavior.

The bundled storage, environment, and workflow modules are enabled by the normal launcher. Their operations still require explicit project policy and scopes. Storage budgets are advisory; reusable environment builds consume ordinary reviewed or budgeted Slurm jobs; workflow nodes use the same ledger and job caps. [Resumable file transfers](docs/data-transfer.md) support inputs and outputs up to 8 GiB per file, and running jobs expose incremental stdout/stderr. Native Slurm arrays, other schedulers and packed environment migration remain planned.

The package shares your operating-system account. Passwordless mode treats local access to the portal as your authority. A process that controls your account can also inspect its files and SSH sessions; this installation does not isolate secrets from such a process. Project API scopes and approval pages govern broker requests, while complete account-level security requires a separate operating-system boundary. See [security](docs/security.md).

## Develop and test

From a source checkout:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -c requirements.lock '.[test]'
python -m pytest -q
python tools/check_contracts.py
python tools/local_smoke.py
python tools/package_smoke.py
```

The default suite uses SQLite and synthetic credentials. It requires no Docker, institutional network, SSH key, or real cluster. A disposable Slurm Docker fixture remains available for [optional integration acceptance](docs/container-testing.md).

The former multi-user server, LDAP, and PostgreSQL implementation is retained on `codex/archive-multi-user-server` at `520effc`; it is outside this package's supported runtime. No live server data is automatically imported or changed by this source cleanup.

## More detail

| Document | Contents |
| --- | --- |
| [Runtime](docs/runtime.md) | Launcher, persistence, job lifecycle, and supported limits |
| [Installation and recovery](docs/deployment.md) | Local configuration, first setup, backups, and restart |
| [Portal](docs/management.md) | Cluster setup, project keys, discovery, and environment recipes |
| [Python environments](docs/python-environments.md) | Module Python, Conda/Poetry selection, worker inspection, and agent-requested venv builds |
| [Architecture](docs/architecture.md) | Components, immutable approval, ledger, and execution boundaries |
| [Backends](docs/backends.md) | Slurm, SSH, remote helper, and reuse assessment |
| [Modules](docs/extensions.md) | Storage, environment builds, and workflows |
| [Investigations and completion](docs/investigations.md) | Bounded plans, optional grouping, status waits and verified run manifests |
| [Testing](docs/testing.md) | SQLite, package, and optional Slurm acceptance |
| [Files and live output](docs/data-transfer.md) | Large uploads/downloads, interruption recovery, output collection retries, and following job logs |
| [Agent recovery and availability](docs/agent-recovery.md) | Recover project jobs/results after restart and compare current cluster queue/hardware observations |

## License

Compute Relay is licensed under the [Apache License, Version 2.0](LICENSE).
Third-party components remain subject to their respective licenses.

## Public release

The intended distribution has not been published. Before publication, maintainers must establish a private security-reporting channel, and release checks must pass. See [contributing](CONTRIBUTING.md) and [security reporting](SECURITY.md).
