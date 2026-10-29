# Local installation and recovery

`compute-relay` starts the loopback portal and saved SSH connectors together.
The local installation retains its configuration and job state between runs.

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then use
an isolated tool environment with Python 3.13 (Python 3.12 is also supported):

```sh
uv tool install --python 3.13 git+https://github.com/schmitfe/compute-relay.git
compute-relay
```

If `compute-relay` is not found, run `uv tool update-shell` and reopen the
terminal. See [uv tool management](https://docs.astral.sh/uv/guides/tools/) for
its installation and upgrade commands. Stop the launcher before replacing its
installed environment, and follow the alpha fresh-setup policy below.

The distribution has not been published yet. Ordinary `python -m pip install .`
from the checkout, a repository URL, or a built wheel remains supported inside a
virtual environment. All ordinary runtime dependencies are included; uv is a
packaging tool, not a runtime requirement. The checkout's `uv.lock` controls
development, CI and disposable fixture installs; installing a wheel or repository
URL as a tool resolves its declared package dependencies. For a checkout run,
use `uv sync --locked` and `uv run --locked compute-relay`. Docker is only needed
for optional Slurm acceptance fixtures.

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

## Alpha updates and fresh setup

This development alpha supports fresh setup rather than migration from earlier
alpha database or configuration formats. Finish or cancel outstanding jobs and
reconcile their accounting and outputs with the existing installation first.
Then stop the launcher and connectors, retain any results and backup you need,
and initialize the updated package in a new private data directory. Reconnect
clusters and create new projects and keys. Existing client workspaces must be
initialized with those new keys. Do not mix an earlier database or connector
state with the new installation, or erase state while submissions are uncertain.

A restart of the same version still preserves jobs, reservations and journals.
The current `slurm-jobs/v1` management path verifies the original manifest,
journal and native identity. Changes to installed helper code cannot turn a
submission intent into a second submission; new submissions require the current
prepared package. Stored helper source is never executed during recovery.

## Manual file cleanup

Use **Preview file cleanup** on a project or job page while the portal is
running. Review the selected batch, save and verify any results you need, then
confirm the local or remote action separately. Confirmation expires after ten
minutes and is rejected if the selected files or references change.

Local cleanup previews up to 100 objects with their file lengths and recorded
storage charges. It protects active/uncertain executions, unfinished accounting
or collection, active transfers, uploaded inputs, job/definition references and
optional-module provenance. It removes eligible result attachments before
unlinking their owned regular files, retaining a durable deletion intent and
storage charge until completion. An interrupted batch can be previewed again to
resume; it never grants new input authority to deleted bytes. Jobs, prepared
packages, bounded logs, submission journals and accounting remain recorded.

Remote cleanup previews up to 100 ordinary Slurm attempts and queues work for
their existing connector. It can wait while that connector is offline. The
worker rechecks authority, exact native/journal/manifest identity and terminal
scheduler evidence before removing an owned attempt directory. It never follows
symlinks, crosses filesystems or deletes managed environments/module roots.
Submission journals remain, so interrupted deletion and lost replies cannot
turn into another compute submission. Remote lengths are measured by the worker
when it runs; the portal does not promise remote free-space savings in advance.
Once remote cleanup is queued, output recovery cannot reopen those directories.

Cleanup is explicit; there is no age-based deletion service. Uploaded inputs,
managed environments, unindexed filesystem orphans, backups and SQLite/WAL
allocation are not reclaimed by these buttons. Local object byte/object quotas
are released once deletion is complete. This is separate from metadata
compaction below; increasing compute allowance does not enlarge either quota.

## Inspect and compact local metadata

With the launcher and connectors stopped, preview a bounded maintenance batch:

```sh
compute-relay maintain-state --data-directory /path/to/private/broker-data \
  --archive-drafts --compact-idempotency --older-than-days 30 --limit 100
```

Add `--apply` to apply those selections. The default is inspection only; `--limit`
accepts 1 through 1000 records per category, and the default cutoff is 30 days.
The command uses the installation lock and does not migrate a pending schema.
Use the matching installation version; initialize fresh state after an
incompatible alpha update.

Draft archival selects only old core draft/prepared jobs that never had an
execution request, are not owned by an extension, and are not live revision
parents. It keeps a count-capped identity and original manifest digest, removes
the executable metadata/file references, and makes the job non-executable. It
does not delete execution history, shared object bytes or remote cluster files.
Idempotency compaction removes old response bodies while retaining the request
identity and hash; replay returns `IDEMPOTENCY_RETIRED`, never another execution.
Retained identity counts are not reset by compaction.

An applied batch also recovers eligible expired upload reservations. Fifteen
minutes of age alone is insufficient: recovery must acquire the same project
writer lock, so it skips a live or paused writer. It verifies any already
published object before finalizing bytes, or aborts an unpublished reservation.
Corrupt objects remain charged for inspection. The reported metadata bytes are
logical admission charges, not SQLite/WAL file size or guaranteed disk space
reclaimed; this command does not run `VACUUM` or object garbage collection.
Busy or failed rows receive a 30-second recovery backoff so later eligible
uploads can progress. Draft selection excludes protected revision parents
before applying the batch limit.

This same-account package does not protect files or authenticated SSH sockets
from another process with the same account's access. See [security](security.md)
for the actual boundary and [runtime](runtime.md) for execution limitations.
