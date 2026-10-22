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
2. Enter the Slurm login hostname, remote username, SSH port and proposed remote
   job directory. You may initially save without the username or directory, but
   both are needed before starting the connector.
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
`--choose-identity` to the startup or resume command. Keep the terminal available
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
read-only discovery. Reload the portal to see the saved result. The first
successful discovery completes the connection setup; it does not grant project
access or permit automatic compute spending. Review the site resource/timing
assumptions, add environment recipes and explicitly allow the connection in a
project before submitting work.

## Resume and reconnect

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

Advanced operators can still register and bind workers manually from the setup
page's **Advanced** section. That path retains its explicit profile/target/root
checks and is useful for simulators and managed installations.
