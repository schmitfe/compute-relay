# Review fixes integrated on 2026-10-24

This note records the implementation boundaries and corrected regression
coverage for findings A01 through A09. It contains no private cluster evidence
or original review material. The integrated changes were checked with the full
suite, a clean installed wheel and disposable native Slurm acceptance. The
recorded results below describe those checks; they are not a complete security
audit.

## Behavior and regression coverage

| Finding | Integrated correction | Corrected validation |
| --- | --- | --- |
| A01: hash reuse bypassed artifact access | Inputs retain an explicit upload grant or exact producer-artifact source. Current credential, artifact-read scope and producer-connection access are rechecked across jobs, packages, revisions, definitions and replay. Internal generated/output storage does not grant independent input authority. | [`test_input_provenance.py`](../tests/unit/test_input_provenance.py), plus environment/workflow source-authority cases. Tests distinguish unreadable outputs from explicitly uploaded copies of identical bytes. |
| A02: failed cancellation lost intent | Backends and the shared lifecycle map explicit accepted, unknown, terminal and identity-mismatch results. Unknown cancellation preserves intent/reservation and observes with retry/backoff. Mismatch surfaces an error and prevents repeated native cancellation until an explicit new request. | [`test_worker_lifecycle.py`](../tests/unit/test_worker_lifecycle.py) runs both workers; [`test_lifecycle_control.py`](../tests/unit/test_lifecycle_control.py) checks controller state, native identity, repeated failures and exactly-once settlement. |
| A03: helper edits stranded existing attempts | New submission still requires the approved execution-bundle SHA-256. Current compatible management verifies the original package and remote journal/native identity without running archived source. Supported management formats are the original unversioned `slurm-ssh/v1` and explicit `slurm-jobs/v1`. | [`test_remote_bundles.py`](../tests/unit/test_remote_bundles.py) exercises sync/native-async cross-version observation, cancellation, reconciliation, collection, final accounting and uncertain recovery without a second submission. [`test_upgrade_check.py`](../tests/unit/test_upgrade_check.py) covers the offline preflight. |
| A04: ineligible submissions hid later running jobs | The database excludes ineligible undispatched candidates before the bounded slice. Management precedes submission; its oldest due work wins, with cancellation/reconciliation tie preference. Existing dispatched work remains eligible when admission is paused or disabled. | [`test_lifecycle_control.py`](../tests/unit/test_lifecycle_control.py) places more than 30 paused, disabled or unsupported submissions ahead of management work, and proves slow failed cancellations cannot starve overdue observations. |
| A05: durable metadata escaped blob/compute quotas | Transactional logical byte charges and count caps cover jobs, idempotency responses and combined extension definitions/runs. Audit retention and per-attempt report receipts are bounded. Explicit stopped-instance maintenance archives eligible never-executed jobs and compacts response bodies while retaining replay fences. | [`test_metadata_limits.py`](../tests/unit/test_metadata_limits.py) and [`test_metadata_maintenance.py`](../tests/unit/test_metadata_maintenance.py) check counts, byte ceilings, concurrent writers, rollback, tombstones and continued worker accounting at capacity. |
| A06: failed file publication leaked reservations | Directory creation and all later publication steps are inside recovery coverage. Reservations have a digest, unique temporary identity and 15-minute expiry. A per-project OS writer lock prevents recovery racing a live/paused writer. Published bytes are verified and charged once; missing unpublished uploads abort. | [`test_upload_recovery.py`](../tests/unit/test_upload_recovery.py) injects directory failures, simulates crashed publication and missing/corrupt objects, and proves an expired live writer is not reclaimed. |
| A07: slow work expired before final accounting could report | A 120-second lease renews every 20 seconds up to a fixed 40-minute generation deadline. Terminal evidence is journaled before hooks/collection; ready reports replay before new leases. Current equivalent management can rebind terminal evidence after replacement, while stale generations remain fenced. | [`test_worker_lifecycle.py`](../tests/unit/test_worker_lifecycle.py) and [`test_lifecycle_control.py`](../tests/unit/test_lifecycle_control.py) cover slow collection, failed renewals, interrupted hooks/collection, lost acknowledgements, superseded generations and bounded receipt retention. |
| A08: one source file coupled discovery to execution | Focused filesystem, scheduler, journal, collection and discovery components form deterministic, stdlib-only operation bundles. Catalog-only changes leave execution identity unchanged. Only fixed installed component names are assembled. | [`test_remote_bundles.py`](../tests/unit/test_remote_bundles.py) verifies determinism, fixed source coverage, stdlib imports, isolated execution and discovery independence. [`package_smoke.py`](../tools/package_smoke.py) checks installed runtime resources/bundles as part of clean-wheel acceptance. |
| A09: lifecycle rules drifted between workers | [`domain/lifecycle.py`](../src/compute_relay/domain/lifecycle.py) owns typed operation/outcome contracts, package validation, submission planning, cancellation mapping, identity checks and report replay. Sync/async adapters retain their own I/O; controller settlement stays explicit. | The shared cases in [`test_worker_lifecycle.py`](../tests/unit/test_worker_lifecycle.py) run against both adapters. Invalid operation/outcome pairs are rejected by worker and controller tests. |

## Upgrade and recovery contract

Stop the launcher and separately started connectors, retain the previous code,
and back up the complete installation state before the candidate launcher can
migrate it. Run the candidate's read-only preflight:

```sh
compute-relay check-upgrade --data-directory /path/to/private/broker-data
```

The command opens SQLite read-only under the installation lock. It does not
migrate, submit, authenticate SSH or execute saved code. Unsupported outstanding
management protocols require draining/cancelling and settling through the old
compatible version before upgrading. A changed execution hash on unsubmitted
prepared work requires a new reviewed revision. A prior durable submit intent
always reconciles its original attempt; it cannot become a new submission.

Old inputs without verified provenance require explicit re-upload in a new
revision or a currently readable artifact. Existing reservations without enough
publication metadata require inspection; absence of a recoverable digest is not
permission to refund bytes. Restore matching code and the complete database/
object-store backup for rollback, and account for cluster jobs accepted after
the backup before reopening admission. Detailed procedures are in
[deployment](deployment.md#check-an-upgrade-before-starting-it).

## Capacity and retention contract

Metadata admission defaults to 32 MiB of logical charges per project and 256 MiB
per installation, with separate record-family limits. These charges are not
SQLite/WAL or filesystem byte accounting. Stored objects separately default to
64 MiB per project and 256 MiB globally. Keep disk/inode headroom and backups;
these controls do not impose quotas on unrelated NAS writers or remote jobs.

`compute-relay maintain-state` defaults to a dry run. An explicit `--apply` can
archive selected old core draft/prepared jobs that never had execution requests,
compact old idempotency response bodies, and recover a bounded expired-upload
batch. Archived identities and idempotency request hashes remain count-capped
tombstones; neither operation permits replay of old execution. It does not
erase attempts, ledger history, shared blob bytes or remote files. See
[runtime capacity](runtime.md#local-storage-and-metadata-bounds) and
[maintenance](deployment.md#inspect-and-compact-local-metadata).
Draft eligibility excludes active revision parents before the bounded slice;
busy or failed upload recovery uses a durable 30-second backoff. Repeated
maintenance calls therefore reach later eligible records.

Terminal evidence survives slow hooks and collection in the worker journal.
The controller retains 16 recent receipts per attempt; pruning a receipt does
not make its old lease current again. Changed extension context drops cached
extension metadata while preserving eligible core accounting. Failed output
storage or finalization cannot turn a definitive allocation into a refund.

## Validation boundaries

The default suite uses synthetic accounts, stub schedulers, isolated fixed
helper subprocesses and controlled clocks. It checks failures without filling a
real disk or running institutional jobs. The clean installed-wheel check verifies
bundled sources, installation, actual local HTML serving, shutdown and same-state
restart. The optional native [upgrade overlay](container-testing.md#native-helper-upgrade-acceptance)
submits tiny disposable Slurm jobs under version N and manages them under N+1;
its result is recorded separately. The ordinary/personal connector smoke
remains the separate HTTP approval and ledger-settlement check.

Recorded local acceptance on 2026-10-24:

- The full integrated suite passed **2,065 tests** on both Python **3.12.3** and
  **3.13.9**, each with **1 skipped** and **67 subtests passed**.
- Four contract schemas and four examples validated offline. Bundled migrations
  matched the models without further generated changes.
- A clean wheel and source distribution passed completeness/dependency checks
  for 205 runtime files. The installed command served its HTML portal, stopped
  on Ctrl-C, and restarted using the same SQLite state and owner.
- Reviewed and budgeted HTTP smoke flows passed using the synthetic backend.
- The native helper-upgrade overlay passed reconciliation, cancellation,
  observation, collection and final accounting, including one deliberately lost
  reply and exactly two native submissions for two jobs.
- The native personal-connector smoke passed pairing, OpenSSH master reuse,
  discovery, human review, execution, artifact collection, accounting settlement,
  resumed identity, directory browsing and revocation cleanup.
- Isolated system tests migrated a populated v1 database without losing
  accounting or inventing upload grants, and recovered an upload after a real
  child-process exit between publication and finalization.

The optional Slurm CI workflow now includes the upgrade overlay. Local fixture
containers and their volumes were removed after acceptance.

## Python 3.12 CI cancellation-test correction

The earlier CI failure checked that a descendant's `/proc/<pid>/stat` existed
before opening it. Linux could reap the stopped process between those actions,
raising `ProcessLookupError` even though cancellation succeeded. The test now
reads once and accepts only a missing/reaped process or zombie state. A running
process still fails, and permission or unrelated I/O failures remain visible.
Eight deterministic cases cover those alternatives in
[`test_async_transport.py`](../tests/ssh/test_async_transport.py). Production
transport behavior is unchanged by this test correction.

The local same-account and remote same-UID limitations remain unchanged. These
fixes do not provide host-level secret isolation, hostile workload containment,
physical disk guarantees or a cluster-wide compute cap. See
[security](security.md).
