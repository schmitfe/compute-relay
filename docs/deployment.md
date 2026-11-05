# Local installation and recovery

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and use a
[tested Python](testing.md#ci):

```sh
uv tool install --python 3.13 git+https://github.com/schmitfe/compute-relay.git
compute-relay
```

If the command is missing, run `uv tool update-shell` and reopen the terminal.
The distribution is not yet published. Pip installation from checkout/repository/
wheel in a virtual environment also works; all runtime dependencies are included
and uv is not required at runtime. Checkout runs use `uv sync --locked` and
`uv run --locked compute-relay`; `uv.lock` governs development/CI/fixtures.
Docker is only for optional native acceptance. Stop the launcher before replacing
its installed environment; incompatible updates follow fresh setup below.

## First launch

The launcher opens `http://127.0.0.1:8768`. Local account `local` can use a
confirmed password or explicitly chosen passwordless mode; that choice persists.
Passwordless access still exchanges a private launch link for a browser session.
Protect that link as a password; [security](security.md#local-human-authority)
describes its limits.

For unattended first setup:

```sh
compute-relay --non-interactive --no-password --no-browser --no-connectors
```

Alternatively supply your own private `0600` file with `--owner-password-file`.
The package neither prints nor generates plaintext passwords. Remove the file
when no longer needed. Restart preserves existing identity/login settings.

## State and configuration

Private local state defaults to `$XDG_DATA_HOME/compute-relay`, or
`~/.local/share/compute-relay`: database, objects, configuration and connector
registry. Another directory is a separate installation, not a migration.

```sh
compute-relay --data-directory /path/to/private/broker-data --port 8770
```

| Option | Purpose |
| --- | --- |
| `--data-directory PATH` | Durable private state |
| `--port N` | Initial loopback port, default 8768 |
| `--extensions LIST` | Bundled modules; empty selects core only |
| `--registry-directory PATH` | Explicit alternate connector registry |
| `--no-browser` | Do not open a browser |
| `--no-connectors` | Start portal alone |
| `--non-interactive` | Refuse terminal-input steps |
| `--owner-password-file PATH` / `--no-password` | Explicit login choice |
| `--verbose` | Sanitized operational messages |

Port/module selection is saved. Existing origins/bindings require the saved port;
use another data directory for a separate port. The normal launcher includes
storage/environments/workflows; change modules only after checking their
[retention requirements](extensions.md#enablement-and-retention).

## Cluster sessions

[Add a cluster](management.md#add-a-cluster), then start its connector in the
launcher terminal for SSH prompts. Only this installation's saved connectors are
started. [Reconnect](personal-connectors.md#resume-and-reconnect) explains expiry,
owner-permitted renewal and standalone commands.

## Stop, restart, and back up

Ctrl-C stops local services/SSH masters, not accepted Slurm jobs. Restart the
same installation/state to reconcile their existing identities and accounting.
Never replace an uncertain attempt with another submission.

For backup, stop the launcher and copy the entire state plus separately stored
connector directories, preserving permissions. SSH keys need their own backup.
Restore while stopped, reconnect/reconcile before admitting work, and retain one
authoritative ledger. A stale backup can omit later submitted jobs; starting
competing copies cannot repair that evidence.

## Alpha updates and fresh setup

Incompatible alpha database/configuration upgrades are unsupported. Finish or
cancel outstanding jobs and reconcile accounting/outputs using the old version.
Stop services, retain needed results/backups, then initialize the updated package
in a new private directory. Reconnect clusters and recreate projects/keys/client
connections. Never mix old state or erase uncertain submission evidence.
Run control and connectors from the same installed version and restart MCP.
Same-version restart preserves reservations/journals; current compatible helper
management verifies original identity and never executes stored helper source.

## Manual file cleanup

Use **Preview file cleanup** on a project/job, verify wanted results, then confirm
local and remote actions separately. Confirmations expire after ten minutes and
reject changed files/references.

Local batches preview at most 100 objects with lengths/charges. Active/uncertain
execution, accounting/collection, transfers, uploads and job/module references
protect objects. Deletion records intent and retains the charge until completed;
interruption resumes through a fresh preview. Jobs, packages, logs, journals and
accounting remain. Deletion never grants input authority.

Remote batches queue at most 100 ordinary Slurm attempts for the existing worker,
which rechecks authority, terminal evidence and exact journal/manifest/native
identity. It removes only owned attempt trees, without links/filesystem crossings;
managed environments and registered roots/module paths protect overlaps, even
when registered after queuing. Protection spans connections sharing normalized
host/account/port; profile names are not isolation, and DNS aliases are not inferred.
Pending canonical path registration and cleanup exclude each other.

Journals survive deletion; lost replies cannot create another submission. Remote
lengths are measured during work, not promised in the preview. Once queued,
collection cannot reopen those directories. Cleanup may wait for an offline worker.
No automatic age deletion exists. Uploads, environments, unindexed orphans, backups
and SQLite/WAL allocation are outside these buttons. Object quotas release only
after successful deletion; metadata compaction is separate.

## Inspect and compact local metadata

Stop launcher/connectors and preview:

```sh
compute-relay maintain-state --data-directory /path/to/private/broker-data \
  --archive-drafts --compact-idempotency --older-than-days 30 --limit 100
```

`--apply` executes; otherwise inspect only. Limits are 1–1,000 per category,
cutoff defaults 30 days. The installation lock and matching version are required;
this does not migrate schemas.

Archival selects only old core draft/prepared jobs with no execution request,
module ownership or live revision-child dependency. It keeps capped identity/
manifest evidence but removes executable metadata/references, never execution
history, object bytes or remote files. Idempotency compaction removes response
bodies while retaining identity/hash; replay yields `IDEMPOTENCY_RETIRED`, not
another execution. Retained identity counts remain charged.

Applied batches also recover expired uploads only after acquiring their writer
lock: age never overrides a live writer. Verify published objects before finalizing,
abort unpublished reservations and keep corrupt state charged for inspection.
Busy/failed rows back off 30 seconds; protected parents are excluded before the
batch limit. Reported metadata charges are logical, not SQLite/WAL size or promised
space savings; no `VACUUM` or orphan-object garbage collection runs.
