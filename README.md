# Compute Broker

Compute Broker lets AI clients prepare, submit, and inspect compute jobs on configured clusters. Projects use human approval, a shared compute allowance, or both. Its first backend is Slurm over SSH. A thin MCP facade gives agents project access while the worker holds cluster access.

**Status: development alpha.** The Django control service, project/key/cluster management portal, review pages, transactional ledger, durable worker, Slurm/SSH adapter, simulated backend, and stdio MCP tools are implemented. Projects share compute and submission limits across keys; each key can require human review. Keys are shown once and stored only as hashes. Disposable native and Docker workflows exercise management, approval, rejection, revision, execution, collection, and accounting. See [runtime scope and limitations](docs/runtime.md) and [container acceptance results](docs/container-testing.md) for what has actually been tested. Protected personal/server deployment, institutional MFA acceptance, and hostile-workload isolation are not release-ready. The working name is provisional.

Optional [extensions](docs/extensions.md) add advisory storage planning, scheduled
reusable Python environment builds, and artifact-dependent workflows. They have
separate apps, tables, routes and tests, and are disabled by default. Ordinary
submission and accounting work without them. Remote filesystem migration,
packed environments and [native arrays](docs/arrays-design.md) remain planned.
The [fresh-agent science kit](examples/agent-integration/README.md) provides five
copyable tasks and private project configuration generation for client acceptance.

## Try it

Use Python 3.12 or 3.13. This runs an isolated fake workflow, creates only synthetic credentials, starts the service and worker, and removes its temporary state afterward:

```sh
python -m venv .venv
.venv/bin/python -m pip install -c requirements.lock '.[test,mcp]'
.venv/bin/python -m pytest -q
.venv/bin/python tools/local_smoke.py
.venv/bin/python tools/local_smoke.py --management
.venv/bin/python tools/local_smoke.py --mode budgeted
```

For an interactive dashboard and an actual disposable Slurm cluster, follow [the Docker instructions](docs/container-testing.md). The ordinary test suite never discovers or submits to a real cluster.

Development mode does not protect secrets from another process running as your account. A real deployment requires a separate service identity or server that the agent cannot administer. An agent that controls Docker can inspect containers and their secrets.

## Design decisions

- Build the project permissions, immutable approvals, compute reservations, and audit history in our own control service.
- Implement a small Slurm adapter using OpenSSH first. Evaluate FirecREST behind the same interface after proving its fit for the university MFA flow.
- Keep cluster credentials in a protected worker. The process launched by an AI client holds only its project credential.
- Run the local service under a separate protected operating-system identity. An agent with no root, sudo, or Docker-daemon access must not read the service's secret files or alter deployed code.
- Use Django for the control service, persistent SQL attempts and leases for work, and the official MCP SDK for the client facade. SQLite is implemented and tested; PostgreSQL/shared-server support remains planned.
- Support single-node, bounded jobs initially. Arrays, automatic requeue, arbitrary scheduler directives, other schedulers, and autonomous agent orchestration are later features.

The service governs jobs submitted through it. Arbitrary code running under a cluster account can potentially submit other jobs, change same-user files, or modify queued jobs. A hard ceiling on all activity under that account requires cluster-side restrictions. See the [security model](docs/security.md).

## Read the plan

| Document | Contents |
| --- | --- |
| [Architecture](docs/architecture.md) | Components, trust boundaries, data model, state machine, budget transactions |
| [Implemented runtime](docs/runtime.md) | Entrypoints, configuration, API, modes, current restrictions |
| [Portal and worker profiles](docs/management.md) | Projects, one-time API keys, cluster enrollment, software discovery and environment recipes |
| [API and contracts](docs/api.md) | REST and MCP operations, worker protocol, errors, examples |
| [Security](docs/security.md) | Threat model, protected identities, secrets, MFA, approval limits |
| [Backends](docs/backends.md) | Slurm implementation, SSH helper, environments, reuse assessment |
| [Deployment](docs/deployment.md) | Personal and hosted deployment, bootstrap, recovery and migration |
| [Implementation](docs/implementation.md) | Proposed source tree, milestone work packages and acceptance gates |
| [Testing](docs/testing.md) | Test IDs, layers, failure injection, cluster acceptance and release gates |
| [Decision records](docs/adr/README.md) | Decisions and their consequences |
| [HPC-MCP source assessment](docs/references/hpc-mcp.md) | Pinned upstream review, reusable components and required adaptations |
| [RASER paper assessment](docs/references/paper-2609-03598.md) | Future runtime boundary and resilience test implications |

## Check the planning contracts

Use Python 3.12 or 3.13 in an isolated development environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-plan.txt
.venv/bin/python tools/check_contracts.py
.venv/bin/python -m unittest discover -s tests/contracts -v
```

These separate checks validate schemas, examples, and contract-level invariants. The pytest suite also exercises database authorization/concurrency, worker recovery, SSH/helper boundaries, and the MCP protocol. Real Docker/Slurm acceptance is explicit; MFA and deployment-security gates remain in the [testing plan](docs/testing.md).

Example contracts use synthetic UUIDs, filenames, and hashes. They contain no real hosts, usernames, keys, or datasets. Keep real discovery snapshots, configuration, credentials, and logs in an ignored private directory such as `local/`.

## Public release

The target is a public source repository with documented operation, contribution guidance, and automated checks. Nothing has been published. Before release, the rights holder must choose a license, maintainers must establish a private security-reporting channel, and the runtime must pass its release gates. No redistribution license has been granted yet. See [contributing](CONTRIBUTING.md) and [security reporting](SECURITY.md).
