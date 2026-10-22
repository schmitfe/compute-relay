# Offline SQLite to PostgreSQL migration

`tools/migrate_control_db.py` transfers an already quiescent installation. It
never pauses a live service, stops a worker, edits deployment settings or switches
the active database. The operator owns those cutover steps. Keep the source and
target from accepting work simultaneously, including during rollback.

The export contains password hashes, session contents, token hashes, retained
logs and private metadata. Treat it as a database backup. The tool accepts only
private input files owned by the operator, and creates new export/report files
with mode `0600` in an operator-owned `0700` directory under ignored `local/` or a
temporary directory. It refuses existing output files and symbolic links. Never
commit, upload to an issue, or paste the export into an agent conversation.

## Prerequisites and refusal conditions

1. Use the same reviewed code and exact Django version for source and target.
   Enable every module whose tables exist in the source, even if the module was
   previously disabled. Unknown tables or migrations cause refusal rather than
   silent omission. A separate rehearsal should prove the enabled module set.
2. Pause every project, then stop all controllers, workers, maintenance jobs and
   other writers. Finish or safely resolve outstanding work through the normal
   broker before taking the backup. The tool will not repair it or waive checks.
3. Every attempt must be terminal and have settled or released accounting. There
   must be no pending execution approval, active execution lease, queued/in-flight
   discovery, pending upload reservation, nonterminal workflow, active workflow
   claim, unfinished environment finalization or leased storage observation.
   Storage capacity estimates in `held` state are retained planning records;
   they are not a remote operation and may be copied.
4. Compute balances must match the reservation/final ledger pairs and have zero
   outstanding reservations. Project/global blob counters must match the retained
   objects and have no outstanding byte reservation. Repair inconsistencies with
   a reviewed procedure before migration, then create a new backup.
5. Produce a standalone SQLite backup using SQLite's backup API or an established
   offline backup procedure. Do not copy a live database file while ignoring its
   WAL. The tool refuses WAL, SHM or journal sidecars and opens the supplied backup
   with `mode=ro&immutable=1`; it checks integrity, foreign keys and a before/after
   file digest. The immutable option is safe only because the operator has
   supplied a stable backup, as described by [SQLite URI parameters](https://www.sqlite.org/uri.html).
6. Preserve the original `django-secret` file in protected storage. Copy it
   separately if the target is on another host. The tool exports only its
   fingerprint and verifies that the configured target uses the same value.
   This preserves the validity of existing signed sessions; passwords and user
   UUIDs are copied exactly. Worker private configurations and SSH credentials
   remain on their protected workers and are not part of the database export.
7. Copy the complete retained artifact directory separately with its private
   ownership and permissions. The tool verifies every referenced blob's size and
   SHA-256, including job inputs and generated files, before export and inside
   target verification. It does not copy the bytes or include them in JSON.

`--offline-confirmed` acknowledges these operational prerequisites. It does not
override any unsafe-state check. Reconcile uncertain native submissions on the
source first; changing databases must never become permission to resubmit them.

## Verify quarantined legacy key owners explicitly

Migration `0009` binds historical keys only when a recorded creation or rotation
event identifies their issuing user. An unknown issuer is quarantined with
`issued_by=NULL`, `revoked=True` and a `credential.owner_unresolved` audit event
that retains the previous revocation flag. Current project ownership or the
absence of current memberships cannot establish who originally received a key.

If the operator can establish a key's historical issuing account independently,
write a private mapping file with explicit UUIDs. Do not include token bytes,
passwords, SSH identities or other credentials:

```json
{
  "schema_version": "1.0",
  "owners": {
    "11111111-1111-4111-8111-111111111111": "22222222-2222-4222-8222-222222222222"
  }
}
```

Against the explicitly selected offline database, after applying the provenance
migration and while all projects/processes remain paused:

```bash
python tools/verify_legacy_key_owners.py \
  --mapping local/migration-20261022/verified-owners.json \
  --report local/migration-20261022/verified-owners-report.json \
  --settings compute_broker.control.settings \
  --offline-confirmed
```

The tool supports configured SQLite and PostgreSQL databases. It changes only
quarantined unknown keys, requires an active user with explicit current project
ownership/membership and roles matching every existing scope, and refuses known
creation/rotation provenance, repeated verification, conflicting records, missing
users, or an explicit revocation after quarantine. The entire batch commits
atomically; one invalid assignment rejects every assignment.

An accepted mapping restores the quarantine record's original revocation flag.
A previously revoked key stays revoked. Scopes, restrictions, expiry and token
hashes remain unchanged, and no token is revealed. Each assignment creates a
`credential.legacy_owner_verified` audit record containing the verified account
and mapping fingerprint. This is an explicit operator attestation, not automated
identity discovery. Create a new export after this operation and verify the same
mapping during the final stopped-source cutover; never assume a rehearsal updated
the live installation.

## Export and rehearse a private database copy

Create a private directory, then export from the immutable backup. The paths below
are examples and contain no credentials:

```bash
mkdir -m 700 local/migration-20261022
python tools/migrate_control_db.py export \
  --sqlite-backup /private/offline-backup/control.sqlite3 \
  --secret-key-file /private/offline-backup/django-secret \
  --artifact-root /private/offline-backup/objects \
  --extensions storage,environments,workflows \
  --output local/migration-20261022/transfer.json \
  --offline-confirmed

python tools/migrate_control_db.py rehearse-sqlite \
  --input local/migration-20261022/transfer.json \
  --output-db local/migration-20261022/rehearsal.sqlite3 \
  --secret-key-file /private/offline-backup/django-secret \
  --artifact-root /private/offline-backup/objects \
  --report local/migration-20261022/rehearsal-report.json \
  --offline-confirmed
```

The rehearsal command creates and migrates a **new** disposable SQLite file. It
exercises the same restore, all-table digest and balance checks without modifying
the backup. It does not establish PostgreSQL compatibility, transaction semantics
or production readiness. A failed rehearsal may leave a schema-only database for
inspection; select a new output path for another run.

## Import into an empty migrated PostgreSQL database

Provision a dedicated target database, select it using the deployment's PostgreSQL
settings, and apply migrations with the same enabled modules. Passwords belong
in the protected password file, never command arguments or a DSN. Set
`BROKER_SECRET_KEY_FILE` to the preserved original secret and
`BROKER_ARTIFACT_ROOT` to the copied retained objects. Apply migrations while the
service and workers remain stopped:

```bash
python -m django migrate --settings=compute_broker.control.settings
python tools/migrate_control_db.py import \
  --input local/migration-20261022/transfer.json \
  --settings compute_broker.control.settings \
  --secret-key-file /private/target/django-secret \
  --artifact-root /private/target/objects \
  --report local/migration-20261022/postgres-import.json \
  --offline-confirmed

python tools/migrate_control_db.py verify \
  --input local/migration-20261022/transfer.json \
  --settings compute_broker.control.settings \
  --secret-key-file /private/target/django-secret \
  --artifact-root /private/target/objects \
  --report local/migration-20261022/postgres-verify.json \
  --offline-confirmed
```

Ordinary `import` and `verify` reject SQLite targets. Import also rejects any target
user, group, session, project or domain data. Only the exact bootstrap identities
for installed content types and permissions, plus applied migrations, may be
present. These bootstrap records are replaced transactionally with their original
source primary keys. The source migration history is retained after verifying
that target schema migrations and migration-file fingerprints match.

The restore holds PostgreSQL table locks, restores all rows in one transaction,
checks foreign keys, compares per-table counts and canonical digests, verifies
ledger/storage balances and artifact bytes, and resets integer sequences before
commit. A failed restore rolls back inserted records and restores the target's
bootstrap rows. PostgreSQL sequence counters themselves are not transactional;
reset occurs only after data verification. Do not allow target application
traffic during this process. The separate verification uses a repeatable-read,
read-only transaction.

The fixture uses Django serialization with natural foreign keys where supported,
explicit primary keys everywhere, and explicit rows for automatic many-to-many
tables. This preserves relationship-table keys and gaps. A custom datetime encoder
retains full microseconds. Forward references are resolved before foreign-key
validation. See [Django serialization](https://docs.djangoproject.com/en/5.2/topics/serialization/)
and [fixture loading behavior](https://docs.djangoproject.com/en/5.2/ref/django-admin/#loaddata).

## Acceptance and rollback

Before resuming anything, compare the private reports and check human sign-in,
an existing session, project/key permissions, cluster/environment bindings,
approval comments, prepared package hashes, past logs/artifacts and ledger totals.
Keep every migrated project paused until those checks pass. Resume only the
target controller/worker configuration; do not start source and target together.

The tool emits only success counts and the bundle fingerprint on stdout. Errors
omit SQL, connection parameters, row contents and tracebacks. Reports contain
aggregate counts and checksums; the private export carries the full table
manifests and balances. A SHA-256 checksum detects corruption, not malicious
rewriting by somebody who can alter the protected export.

A report-write failure after a successful database commit is reported separately
as `ok: true`, `report_written: false` and `REPORT_NOT_WRITTEN`. Do not repeat an
import or owner assignment in that situation; inspect the destination and use a
new report path for verification. The database commit is already durable.

If acceptance fails **before** the target admits any work, stop all target
processes and restore the preserved source service configuration and private
files. Once the target has admitted work or changed state, the old source backup
is stale: do not restart it or blindly re-import it. Reconcile native jobs and
make a reviewed reverse migration or recovery plan first. Retain the backup,
artifact copy and worker journals until the retention policy permits cleanup.

The current tool buffers JSON in memory and limits exports to 512 MiB. Larger
installations require a reviewed streaming transfer path. Its ordinary CI checks
use synthetic SQLite data; a disposable PostgreSQL rehearsal is separate evidence
and must pass before an actual cutover.
