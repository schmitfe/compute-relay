# Personal Slurm connectors

A connector runs on your device, holding SSH access for one cluster account.
The normal launcher enrolls its worker automatically; follow the portal's
[add-cluster flow](management.md#add-a-cluster). Project keys and HPC identities
remain separate. The advanced commands below are for standalone connectors.

## Advanced direct connector setup

1. Add a cluster with login hostname, remote username and optional port/work root.
   Without a root, discovery proposes `.compute-relay/jobs` under the canonical
   remote home without creating it. The username is required before login.
2. Leave the key path empty for a local file picker, or enter local key/known-hosts
   paths, absolute or beginning `~/`. These are hints, not uploads. Never paste
   authentication material into the portal.
3. Set the SSH lifetime if needed: 1–1,440 minutes, default eight hours. This cannot
   extend a shorter site-permitted session.
4. **Download personal connector setup**. Keep it private: its owner-bound pairing
   code expires after ten minutes, works once, and a new download invalidates old
   unused codes. Changed cluster details require a new download.

Saving/downloading does not contact SSH, create directories or submit jobs.

## Start on your device

```sh
chmod 600 /absolute/private/personal-connector.json
compute-relay connector \
  --setup /absolute/private/personal-connector.json \
  --state-directory /absolute/private/connector-state
```

Use a dedicated private local state directory; its path plus `/s` must fit in
100 bytes. Avoid whitespace or `%` in SSH paths. Preserve its worker identity,
configuration and journals across restarts. Missing directories are created
`0700`; existing permissions are not silently changed. `STATE_ANCESTOR_WRITABLE`
means a parent permits group/other writes: fix that parent or choose a private
location. Validation precedes key selection, login and enrollment.

The picker returns a path without reading/uploading the key. Cancelling stops
selection. It uses Tk, with Zenity fallback on graphical Linux; headless or
noninteractive startup requires `--identity-file`. To override a saved selection,
use `--choose-identity`, or supply the path:

```sh
compute-relay connector \
  --setup /absolute/private/personal-connector.json \
  --identity-file /absolute/path/to/ssh-key \
  --state-directory /absolute/private/connector-state
```

The key and setup must be ordinary owned private files. The picker changes no
permissions and bypasses no checks. If needed:

```sh
chmod 600 /absolute/path/to/ssh-key
```

OpenSSH asks for host-key confirmation, passphrase and MFA in this terminal.
Verify unfamiliar fingerprints independently with the cluster administrator.
The browser never receives those answers; keep the terminal available.

After login, enrollment publishes a narrow profile, binds the cluster and runs
discovery. Complete [cluster configuration](cluster-configuration.md) in the
portal; worker acknowledgement is required before job access. Configuration
receipts retain their canonical path across retries. If the five-minute request
expires before acknowledgement, explicitly save again; do not delete state or
roll back an uncertain configuration.

A failed initial enrollment reply can consume the one-use code: obtain a new
setup and fresh state directory for that initial pairing. Existing paired
connectors always resume their saved state. Re-pairing retains the connection ID
and known site bounds but waits for all existing execution/recovery work to finish.

## Resume and reconnect

Omit setup on restart:

```sh
compute-relay connector --state-directory /absolute/private/connector-state
```

Named shortcuts and sanitized activity are described below.

The portal shows expiry under **Connect to this cluster**. **Reconnect** drains
that connector and releases its lock before restarting it in the application
terminal; other clusters continue. `STATE_LOCKED` means an existing process owns
the identity: inspect it, never delete its lock/journal to force another start.

**Allow automatic SSH reconnection** is saved per cluster and defaults off.
With permission, an eligible explicit key can renew expiry or recover an outage.
Permission is checked before authentication; failed authentication requires human
reconnect. Renewals start a fresh session lifetime. Background operations never
use ambient agents/default keys, saved passwords, passphrase replay or MFA retries.

Keep the connector running while dispatch, observation or collection is needed.
Stopping it, expiry or a network outage does not cancel accepted Slurm jobs.
Resume the same state to reconcile them; never create replacement jobs or erase
submission intent. Use normal cancellation if you intend to stop compute.

Ctrl-C, SIGTERM, revocation and normal expiry close the owned master after draining
operations. SIGKILL/power loss cannot clean up. Unknown/replaced/live sockets are
protected; inspect stale state through a trusted local operator channel before
removing anything. Detailed retry and lease rules are in [worker recovery](async-workers.md).

## Remove a worker

Use **Clusters → Protected workers → Remove worker**. The verified owner must
wait until linked submission, monitoring, reconciliation and final accounting
finish, even if Slurm already reports completion. Removal revokes the worker,
blocks linked new jobs and invalidates setup tasks/unused pairing files.

Cluster configurations, jobs, outputs and accounting remain. To replace the
worker, download fresh setup and pair a new private state directory, then repeat
discovery/configuration confirmation. Portal removal does not delete local state
or CLI shortcuts; remove those locally only after accounting/recovery is complete.

## Remove a cluster configuration

Use the owner's confirmation page. This archives configuration, never remote files
or accounting. Normal removal waits for accounting and collection; the explicit
**Remove anyway** override retains unresolved jobs and budget holds. See the
[removal consequences](management.md#remove-a-cluster-configuration) before using it.


## Save connector shortcuts

Register an already paired private state directory once:

```sh
compute-relay connectors add lab --state-directory /absolute/private/lab-state
compute-relay connectors add university --state-directory /absolute/private/university-state
compute-relay connectors list
compute-relay connectors start lab
compute-relay connectors start
compute-relay connectors start --all
compute-relay connectors start lab --verbose
compute-relay connector --state-directory /absolute/private/lab-state --verbose
compute-relay connectors remove lab
```

The registry stores names/paths in the local configuration directory, never
credentials or copied state. Without a name, `start` prompts for a selection;
`--all` serializes logins, then runs labeled workers until Ctrl-C. Successful
startup confirms SSH and broker handshake, not job completion. Idle polling is
quiet; verbose output omits credentials, commands, source and workload output.
Removing a shortcut only removes that entry, not its process, worker, files or jobs.
