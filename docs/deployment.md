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
installed environment, and follow the checked upgrade process below.

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

## Check an upgrade before starting it

Install the candidate package into a separate virtual environment, keep the
previous package/wheel available, and stop the launcher and any separately
started connectors. Back up the stopped state before the updated launcher can
apply migrations. Record which package version or commit belongs to that backup.

Run the candidate's offline check against the existing state directory:

```sh
compute-relay check-upgrade --data-directory /path/to/private/broker-data
```

This command takes the installation lock and opens SQLite read-only. It does
not migrate the database, authenticate SSH, submit jobs or execute stored helper
source. Its JSON summary counts outstanding attempts, unsupported management
protocols, prepared jobs requiring a new revision, inputs without verified
provenance, and old upload reservations needing inspection. Unsupported
outstanding protocols cause a nonzero exit; diagnostic samples are bounded.
The check examines stored protocol declarations, not current cluster health or
the integrity of remote files.

The current management bundle supports `slurm-jobs/v1` and the original
unversioned `slurm-ssh/v1` manifest format. It verifies the original manifest
digest and journal/native identity while using current reviewed management code.
It never executes an archived helper supplied by a manifest or journal.
Discovery has a separate bundle, so catalog-only changes do not change the
execution-helper hash. A changed execution bundle still blocks a new submission
of an old prepared package: create and review a new job revision. Existing
submission intent must reconcile its original attempt instead of submitting a
replacement.

If the check finds an unsupported outstanding protocol, use the previous
compatible version to drain or explicitly cancel and settle that work, then
stop, back up and check again. Keep admission paused while doing this; pausing
new work leaves dispatched job management eligible. A passing protocol check
does not remove ordinary authority, budget or input-provenance checks.

Earlier stored files may have no evidence of an independent upload. Such inputs
remain unverified for new use: explicitly upload the bytes and create a new
revision, or select an artifact the current key may read. Do not manufacture
upload grants from hashes. Old reserved uploads without a digest/expiry require
inspection; the automatic recovery path deliberately leaves them unchanged.

For rollback, stop the candidate and restore a matching code **and** complete
database/object-store backup together. Do not run an older binary against a
migrated database or restore only the database while keeping a different object
store. A backup predating cluster submissions cannot establish that those jobs
never ran; reconcile them before reopening admission. See the
the [test guide](testing.md) for compatibility and recovery coverage.

## Inspect and compact local metadata

With the launcher and connectors stopped, preview a bounded maintenance batch:

```sh
compute-relay maintain-state --data-directory /path/to/private/broker-data \
  --archive-drafts --compact-idempotency --older-than-days 30 --limit 100
```

Add `--apply` to apply those selections. The default is inspection only; `--limit`
accepts 1 through 1000 records per category, and the default cutoff is 30 days.
The command uses the installation lock and does not migrate a pending schema.
Complete the checked upgrade first if it reports that migrations are needed.

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
