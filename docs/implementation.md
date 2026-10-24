# Implementation map

The active product is the single-user, pip-installable package described in
[runtime](runtime.md). The former multi-user server scope is archived on
`codex/archive-multi-user-server` at `520effc`. That branch preserves its database
transfer, LDAP, PostgreSQL and deployment work without making them dependencies
of the local application.

## Package boundaries

- The launcher owns local startup, first setup, persistent installation state,
  loopback Waitress serving and foreground connector supervision.
- `control` owns the local human session, project configuration, immutable
  revisions/approvals, SQLite ledger, task leases and bounded HTTP APIs.
- `mcp` provides the official-SDK stdio interface using a project credential.
- `worker`, `backends`, `transports` and `remote` perform verified leased work,
  durable reconciliation and fixed Slurm/SSH operations without direct controller
  database access from the connector.
- `extensions/storage`, `extensions/environments` and `extensions/workflows`
  remain separate capability modules, all included by normal installation.
  Their jobs use core approval, accounting and submission caps.
- `schemas`, migrations, HTML templates, MCP resources and fixed remote helper
  source are runtime package data and must be present in the wheel and sdist.

## Acceptance priorities

A clean package install must start with one command, support a deliberate
password/passwordless choice, preserve state on restart, serialize SSH/MFA
prompts, render every portal screen from installed templates and shut down its
managed processes on Ctrl-C. A portal connector start request must reach that
local terminal, without collecting SSH secrets in the browser.

Job acceptance requires exact package/digest binding, conservative allocation
bounds, atomic project-wide holds and caps, dispatch revalidation, durable
submission intent, no blind retry after uncertainty and definitive accounting
before settlement. Module failures must not undo already committed accounting.

The normal test and package checks use SQLite and loopback only. Disposable
Slurm containers are optional integration fixtures. Institutional acceptance,
OS-level separation from same-account agents, native arrays, alternate
schedulers, packed environments and direct artifact transfer remain independent
future work; this package does not claim those from synthetic tests.
