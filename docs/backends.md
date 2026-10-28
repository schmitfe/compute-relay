# Execution backends

## Current support

The package supports single-node Slurm jobs through OpenSSH. The local control
service owns projects, approvals, budgets, durable attempts and audit history.
A personal connector owns SSH credentials and performs only operations granted
by a current lease; it has no direct database access. The remote helper is
fixed, versioned package code, not an agent-provided command channel.

The backend prepares and hashes the exact batch script before review. Dispatch
rechecks approval and budget, records intent before submission, and treats a
lost submit response as uncertain until reconciled. It never blindly retries an
ambiguous `sbatch`. Final accounting must be verified before reservations are
settled. These invariants apply to all future adapters too.

See [runtime](runtime.md) for shipped behavior and limits,
[personal connectors](personal-connectors.md) for SSH setup,
[asynchronous workers](async-workers.md) for connector recovery, and
[container testing](container-testing.md) for disposable Slurm evidence.

## Supported job shape

A request is one batch attempt with explicit CPU, memory, whole-minute walltime,
and optional whole GPU count/type. Partition, account, QoS and administrator-
approved constraints are validated fields; raw scheduler flags are not accepted.
The worker supports approved module/environment profiles and bounded declared
outputs. Discovery is read-only and does not grant permission: visible
partitions or accounts are not proof that a user may submit to them.

Arrays, heterogeneous or multi-node allocations, interactive jobs, arbitrary
remote commands, agent-controlled retries/requeue, alternate schedulers and
hosted HTTP MCP are unsupported. Direct or object-store artifact delivery is
also unsupported; current files and logs use bounded broker relay. See the
[roadmap](roadmap.md) before proposing new support.

## Adding an adapter

Add a backend only for a concrete deployment need. Keep policy and accounting
in control, and require contract tests for exact prepared bytes, idempotent
identity/reconciliation, cancellation, final allocated usage and explicit
rejection of unsupported options. Preserve the current SSH/MFA and local-secret
boundary; a gateway must not receive authority to approve work or increase
project limits. Record the decision in an [ADR](adr/README.md) after the adapter
passes disposable and permitted site acceptance.
