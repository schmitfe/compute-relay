# Personal Slurm connectors

A personal connector runs on your device and connects your broker account to
your SSH account on a Slurm cluster. The normal setup flow enrolls its worker
automatically. You do not need to register a worker, copy a worker token or edit
its protected profile by hand.

The broker user, project API key and HPC account remain separate identities.
Signing in with a local or LDAP account grants no SSH access. Jobs submitted
with your project key can use only your permitted connection and worker;
sharing a project's allowance does not delegate another user's HPC account.

## Save the cluster and download its setup

1. Sign in to the human portal and open **Clusters → Add a cluster**.
2. Enter the Slurm login hostname, remote username and SSH port. The working
   directory is optional: choose it interactively through the running connector
   after discovery. If omitted, the connector identifies the remote account's
   canonical home and proposes `.compute-broker/jobs` beneath it without creating
   that directory. The username is needed before starting the connector.
3. Leave the SSH key path empty to choose the file with your device's native
   picker at interactive connector startup. You may instead enter SSH key and
   known-hosts **paths on your device**, using an absolute path or one beginning
   with `~/`. These are local hints, not uploads: the portal never reads those
   files. Do not paste a key, password, passphrase or second factor into a field.
4. Choose a requested SSH session lifetime between 60 seconds and 24 hours.
   The default is eight hours. This does not extend a shorter session permitted
   by the cluster.
5. Save, then select **Download personal connector setup**. Keep the downloaded
   JSON private; it authorizes connector enrollment for your account. Do not
   give it to an agent or commit it to a repository. Its pairing code expires
   after ten minutes and can be exchanged only once. A new download invalidates
   older unused setup files for that cluster.

Saving or downloading setup does not contact SSH, create a remote directory or
submit a compute job. The setup is tied to the current saved cluster revision.
If those details change, reload the page and obtain a new setup.

An existing Slurm cluster's overview offers the same download button to its
owner, including connections created through the older manual flow. An
administrator's ability to inspect a cluster is not permission to enroll a
personal connector as its owner. Simulator registrations retain the advanced
manual setup path.

## Start on your device

Use an installed, reviewed broker client with the worker dependencies. In a
terminal on the device that should hold the SSH connection, run:

```sh
chmod 600 /absolute/private/personal-connector.json
compute-broker connector \
  --setup /absolute/private/personal-connector.json \
  --state-directory /absolute/private/connector-state
```

Choose a short absolute path for a private state directory dedicated to this
connector: the directory plus `/s` must fit within 100 bytes. Avoid whitespace
or `%` in SSH paths. Treat the state directory as
credential storage: its configuration, enrollment/worker state and SSH session
capability are not agent inputs. Preserve it across restarts so the existing
worker identity and submission journal can be reused.

Missing state-directory levels are created with private permissions (0700),
independently of a group-friendly default umask. Existing directories are never
silently changed. A `STATE_ANCESTOR_WRITABLE` error means a parent directory
allows writes by a group or other users: remove those write permissions from
that directory, or choose a suitable private location. Validation happens
before the file picker, SSH login or pairing request. Other fixed startup codes
identify expired setup, unreadable or insufficiently private files, and a state
directory already in use, without printing credentials or response bodies.

When no identity path is saved, initial interactive startup opens a native file picker
titled **Select your SSH private key**, initially in your local `~/.ssh` directory.
It returns only the selected existing file's path. It never opens or reads the
key contents and does not send the file to the portal. Cancelling stops selection;
it does not choose a default key or start a second dialog.

The picker uses Tk when available. Linux can fall back to a system-installed
Zenity dialog when a graphical session is present. A headless or noninteractive
session needs an explicit local `--identity-file` path; it never opens a GUI.
Picker tests use mocks, not a real desktop or private key.

OpenSSH performs the initial login interactively in that terminal. It can ask
you to verify a new host key, unlock an encrypted key and complete the site's
second factor, including Cisco MFA during SSH login. Verify the host-key
fingerprint through the cluster's trusted channel. The broker never collects
these answers in the web UI, and the background worker does not blindly repeat
interactive MFA attempts.

To select a different key locally even when a path was saved, add
`--choose-identity` to the first pairing command. Keep the terminal available
for the subsequent SSH prompts. For a session without a graphical picker,
supply a local override:

```sh
compute-broker connector \
  --setup /absolute/private/personal-connector.json \
  --identity-file /absolute/path/to/ssh-key \
  --state-directory /absolute/private/connector-state
```

Your private key must be owned by your account and inaccessible to other users.
The picker does not alter file permissions. If needed, fix the selected key
locally before retrying:

```sh
chmod 600 /absolute/path/to/ssh-key
```

The downloaded setup also needs private permissions, as in the first command.
Selecting a file does not bypass the connector's existing ownership, path and
permission checks; a directory or missing file is not a valid key selection.

After local authentication, the connector enrolls its owner-scoped worker,
publishes a narrow profile for the selected target, binds the cluster and runs
read-only discovery. Reload the portal and open **Configure cluster**. Missing
resource limits are populated from discovered single-node maxima, within the
broker's supported job sizes. Existing owner edits survive rediscovery. The
personal software inventory uses the site's module search paths and a
provisional filename catalog; it does not evaluate modulefiles or load modules.

Select **Browse directories on this cluster** to navigate folders through the
running worker. Only bounded directory metadata returns to your human session;
there is no general remote command or file-content browser for agents. Select
a writable parent and use **New folder → Create and open folder** if the desired
directory does not exist. This creates one private directory (0700), with a
single name made of letters, digits, dots, underscores or hyphens. Existing
directories are opened without changing their contents or permissions; files
and symbolic links are refused. Creating a folder does not change the selected
baseline: select **Use this working directory**, then save the configuration.
An older running connector needs a restart before it offers folder creation.

Select a baseline directory or edit its path, then confirm resource limits and explicit
accounting bounds. The worker validates access without creating files, saves its
new protected scope locally and acknowledges it before job access is enabled.
Changes are refused while execution or accounting is unsettled. Project/key
permissions, environment recipes and execution review still apply afterward.
The cluster configuration also offers an explicit **module avail** discovery
option. Its fixed listing runs as your HPC user and may evaluate site module
configuration. It is disabled by default, persists through restart and does not
expose a general command interface. Save the option, then run discovery again.
See [cluster configuration](cluster-configuration.md) and
[module discovery](module-discovery.md).

After upgrading an older personal connector, restart with the same state
directory. When idle, it refreshes discovery and asks for post-login
configuration confirmation. If old executions still need accounting, their
existing profile is kept until they settle; automatic migration must not move
an active execution to another directory or resource binding.

## Resume and reconnect

For named local shortcuts and labeled startup/activity messages, see
[local client setup](local-client-setup.md). Register a paired state directory
once with `compute-broker connectors add NAME --state-directory PATH`, then use
`compute-broker connectors start NAME`. The original command below continues
to work and accepts `--verbose` for sanitized activity messages.

Resume with the same private directory, omitting the downloaded setup:

```sh
compute-broker connector --state-directory /absolute/private/connector-state
```

Keep the connector available while jobs need dispatch, observation or collection.
Use a terminal where you can complete local authentication when required. A
missing or expired SSH session must be re-established through that interactive
startup; ordinary background operations fail closed rather than falling back to
an ambient agent, default private keys or unattended MFA retries.

Stopping the local connector or losing its network connection does not cancel a
Slurm allocation. Already accepted jobs can continue remotely. Resume the same
state directory to reconcile their native identities and accounting; creating a
new job or deleting local state is not a recovery procedure. Uncertain
submissions retain their reservations until reconciled. Use the broker's normal
cancel operation if you want a submitted job cancelled.

## Remove a cluster configuration

The setup page and cluster overview offer **Remove cluster configuration**.
Its confirmation page is available only to the owner. Removal hides the cluster
from active configuration, blocks new submissions and discovery, invalidates
unused pairing files and clears saved local-path hints. A dedicated personal
connector loses its broker credential, causing its loop to exit and close its
owned SSH session. A shared legacy worker is retained for its other clusters.

Removal is refused while any execution has unsettled accounting. Recorded jobs,
results and accounting are retained; no remote files or software environments
are deleted. A stale confirmation must be reloaded before it can be submitted.

## Boundaries and remaining deployment work

The setup record stores only `identity_file`, `known_hosts` and
`session_seconds` hints. The key and interactive authentication remain local;
the control service never receives an SSH private key. Enrollment and worker
credentials are different from the project API token given to an AI client.

This convenience flow does not establish protected installation isolation. An
agent running as your Unix user may read that user's files or use its active
session capabilities. Use a separately protected identity or device when that
boundary is required. Root/administrator compromise is outside this protection.
Automatic worker enrollment does not provide a central key vault, an isolated
multi-user supervisor or site approval for SSH session reuse.

The connector starts conservatively with human review required. Discovery alone
does not verify a worst-case cleanup bound or justify unattended execution.
Institutional MFA/session policy still needs explicit acceptance on each site;
routine automated tests use synthetic credentials and no institutional cluster.
See [runtime limitations](runtime.md), [SSH/security boundaries](security.md),
[hosted architecture](hosted-architecture.md) and [async-worker compatibility](async-workers.md).

This first personal connector runs core Slurm jobs through the async worker.
Environment-build, workflow and storage extension workers still use the advanced
manual path. Re-pairing an existing connection preserves its ID and known site
bounds but requires it to have no unsettled executions; do not move an active
connection to a new state directory. A failed enrollment response can leave its
one-use code consumed. Obtain a fresh download and use a fresh state directory
for that initial-pairing recovery. Ordinary restarts reuse the saved directory.

Ctrl-C, SIGTERM, broker revocation and the configured session deadline close the
connector-owned SSH master. SIGKILL, host crashes and power loss cannot run that
cleanup. Inspect any stale socket or master through a trusted local operator
channel before removing files; the connector refuses an unknown pre-existing
socket. A local journal with a submission intent must be preserved for recovery.

Configuration acknowledgements are replayable. A locally saved configuration
receipt freezes its canonical path across retries and restarts. If confirmation
does not reach the broker before the five-minute request expires, new execution
stays blocked: open **Configure cluster** and explicitly save it again. Do not
delete local state or roll back an uncertain configuration. Expired account
verification pauses new setup operations while existing jobs continue through
the core observation and accounting path; revoked worker credentials still
close the session.

Advanced operators can still register and bind workers manually from the setup
page's **Advanced** section. That path retains its explicit profile/target/root
checks and is useful for simulators and managed installations.
