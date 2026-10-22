# Container development and disposable Slurm testing

The container stacks exercise the application on a workstation without contacting an institutional cluster. `deploy/compose.fake.yml` runs the broker and worker with the fake backend; `deploy/compose.test.yml` adds a real, disposable CPU Slurm cluster over SSH. These are development/acceptance fixtures, not a production deployment or proof of host-level credential isolation.

The Compose documents use JSON syntax, which is valid YAML, so their basic structure can also be checked with Python's standard library. Docker Compose must still validate and execute them on a host with a working container engine. No container engine is installed by these scripts or tests.

## Layout and identity boundaries

| Component | Image and identity | Storage/authority |
| --- | --- | --- |
| Control | `deploy/Dockerfile`, `control` target, UID/GID 10001 | Own SQLite/artifact volume; owner/project demo metadata; worker enrollment metadata. No SSH-key volume. |
| Worker | Same Dockerfile, `worker` target, UID/GID 10001 | Read-only worker config/token; separate durable journal; fixture-only SSH identity. No control database or human credential mount. |
| Smoke client | Same Dockerfile, `smoke` target, UID/GID 10001 | Read-only synthetic demo metadata; invokes project/key/cluster portal forms, job APIs, and human review endpoints. No SSH identity or database mount. |
| Slurm fixture | `deploy/slurm/Dockerfile`, root for scheduler/SSH initialization | MariaDB, Munge, SlurmDBD, controller, compute daemon, and SSH server in one disposable container. Separate `brokerjob` remote UID runs batch jobs. |

The stack publishes only `127.0.0.1:8765` for the control UI/API by default. Set `BROKER_TEST_PORT` to select another loopback port; internal API ports stay unchanged. SSH, database, Munge, and scheduler ports are internal. The internal Compose network has no default outbound Internet path; builds can download declared Python/Debian dependencies. No service mounts a host Docker socket, a home directory, an existing SSH agent, or institutional keys.

The fixture creates fresh SSH/Munge/database secrets at runtime. The worker-only key volume contains an SSH key and the fixture's directly generated host-key entry. This is fixture provisioning, not trust-on-first-use against a real server. Dockerfile-specific ignore files allowlist the build inputs instead of sending the whole workspace to the builder. The control bootstrap copies only `worker.json` and `worker-token` to the worker-config volume; owner credentials stay in the separate demo-metadata volume. The worker obtains leases/grants and reports through the control API, rather than connecting to its database.

Application services use a read-only root filesystem, private `0700` named data/journal volumes and bounded temporary filesystems, dropped Linux capabilities, and no-new-privileges. The Slurm fixture is intentionally less restrictive to run ordinary distro daemons; it does not request privileged mode or host cgroup mounts. It uses `task/none`, `proctrack/linuxproc`, and an explicit legacy cgroup plugin selection for startup, avoiding Slurm22's automatic cgroup/v2 systemd probe inside the container. No task/proctrack cgroup enforcement is configured. It schedules trusted tiny test jobs and does not demonstrate hostile workload containment or GPU isolation.

An agent that controls the host Docker daemon can inspect these fixture secrets. A protected real installation requires the separate service identity and daemon/admin boundary in [the security design](security.md); same-host Compose alone does not supply that boundary. Ordinary development bootstrap is explicitly synthetic and must not enroll real credentials.

## Build and run

Prerequisites: a working Docker Engine/Compose v2 installation and sufficient local resources for a small database plus Slurm daemons (allow approximately 2 CPU and 2 GiB for the fixture, plus application/build headroom). Start from a clean working copy; Docker uses the application source at build time, with no editable source bind mounts.

The current login session must have daemon access. After adding a user to the Docker group, refresh the login/app session or use a deliberately scoped fresh-group command; do not make the Docker socket world-writable. To avoid a local port conflict, set the same override for the entire workflow:

```sh
export BROKER_TEST_PORT=8767
```

First run the fake stack:

```sh
docker compose -f deploy/compose.fake.yml --profile test build
docker compose -f deploy/compose.fake.yml up -d --wait control worker
docker compose -f deploy/compose.fake.yml --profile test run --rm smoke
```

Inspect state through [the local dashboard](http://127.0.0.1:8765). Synthetic bootstrap credentials are stored in the control service's `/run/demo/demo.json`; avoid copying them into issue reports or logs. The smoke client reads them directly from its fixture-only mount and does not print them.

Stop the fake stack before using the real fixture because both bind the same loopback port:

```sh
docker compose -f deploy/compose.fake.yml down --volumes
```

Then exercise actual Slurm:

```sh
docker compose -f deploy/compose.test.yml --profile test build
docker compose -f deploy/compose.test.yml up -d --wait slurm control worker
docker compose -f deploy/compose.test.yml --profile test run --rm smoke
```

The fixture cluster is `broker-test`, host `slurm-fixture`, partition `debug`, account `broker`, remote user `brokerjob`, and work root `/home/brokerjob/jobs`. It advertises two CPU cores, 1,024 MiB memory, no GPUs, and a maximum ten-minute partition limit. `JobRequeue=0`, `OverTimeLimit=0`, and `KillWait=5` make its test policy explicit. Bootstrap should use only small whole-minute jobs; do not treat its illustrative cleanup allowance as evidence of a finite bound at another site.

Use the fixture's actual scheduler/accounting tools for diagnostics:

```sh
docker compose -f deploy/compose.test.yml exec slurm sinfo
docker compose -f deploy/compose.test.yml exec slurm squeue
docker compose -f deploy/compose.test.yml exec slurm sacct --starttime today --format=JobID,State,ExitCode,AllocCPUS,ElapsedRaw
docker compose -f deploy/compose.test.yml logs --tail 100 slurm control worker
```

Daemon supervision fails the fixture if a critical child exits. Health checks require the controller, an available node, accounting cluster registration, and generated worker SSH material. A healthy fixture does not prove a batch job ran; the smoke workflow must complete and verify its output/accounting.

Remove only this disposable stack's resources when finished:

```sh
docker compose -f deploy/compose.test.yml down --volumes
```

`--volumes` deliberately deletes synthetic job history, tokens, keys, and worker journals. Do not reuse these test project names/volumes for real work. Scheduler/MariaDB state is disposable inside its container; restarting/recreating the fixture with outstanding jobs is a fault test requiring broker reconciliation, not a persistent production cluster. Recreate the entire test stack for a clean run.

## Verification and limitations

### Module job startup and failure details, 22 October 2026

A reviewed CPU-cluster diagnostic exposed a login-only Lmod initialization path
in its environment recipe. Slurm accepted the allocation, but Bash failed before
executing Python because `/usr/share/lmod/lmod/init/bash` was absent on the
compute node. Its collected stderr and final accounting were retained; the
review page previously omitted those logs and scheduler exit status.

Module recipes now default to the cluster-provided module command, using a login
batch shell and restoring the Slurm submission directory after site/account
startup. Their default wrapper sources no explicit initializer. An existing
advanced initializer is only a fallback when the command is unavailable; a
missing command has a fixed, actionable error. New discovery retains optional
initializer provenance without prefilling it as a required recipe setting.
Existing failed packages and environment revisions remain immutable.

The disposable `cb-module-jobs-20261022` fixture installed Debian Environment
Modules and a synthetic Python module. Its complete personal-connector workflow
passed: ordinary reviewed execution, a module recipe with blank initialization,
a normal cluster-check approval, native execution, reported loaded module name,
JSON artifact, settled accounting, same-worker resume and revocation. Test
containers and synthetic volumes were removed afterward.

The owner then approved the corrected lab diagnostic in the portal. It completed
with exit status `0:0`, Python 3.12.4 and all five selected modules. The script
reported two seconds; scheduler allocation elapsed time was four seconds with
two CPUs, so the run spent eight CPU-core seconds. Together with the original
failed allocation's two CPU-core seconds, the existing diagnostic project had
ten spent, zero GPU usage, zero reservations and two lifetime admissions. Its
allowance and counters were preserved rather than reset.

Authenticated HTTP checks confirmed that the failed review shows its specific
cause, native job ID, exit status and escaped logs before prepared files; the
successful review and cluster test page show final results. Review responses
use `no-store`. Default tests cover permission denial, bounded/escaped output,
classification without fabricated causes, use of an existing module command,
explicit fallback behavior and the no-initializer default. Independent review
found no actionable issue in these paths. The final PostgreSQL run passed
**1,249 tests and 65 subtests**, with one optional test skipped and two upstream
LDAP dependency deprecation warnings. Migration drift and whitespace checks
passed; no schema migration was required for the portal update.

### Folder creation, reviewed diagnostics and module catalogs, 22 October 2026

The integrated PostgreSQL suite passed **1,198 tests and 65 subtests**, with one
optional test skipped and two upstream LDAP dependency deprecation warnings.
The subsequent missing/unreadable artifact-preview fix passed its two new GET
regressions within a 70-test diagnostics/environment suite. Migration drift
checks reported no changes.

The separate disposable `cb-folders-checks-20261022` Slurm fixture passed the
personal connector acceptance. Through ordinary authenticated HTTP forms, the
client created one private remote folder, opened it, verified creation alone did
not change the selected baseline, and explicitly saved it as the working root.
It then completed the existing native reviewed Python job and the new cluster
check's prepare → human approval → native execution → hash-verified JSON result
and settled accounting flow. Shutdown, same-worker resume, configuration removal
and SSH-master revocation also passed. The fixture and its synthetic volumes
were removed afterward; persistent development PostgreSQL/LDAP were retained.

Read-only site verification used the already authenticated university connector.
The saved generic inventory contained five utility names. Corrected protected
site-snippet and group-managed directory discovery returned **2,076** filename
candidates. Explicitly authorized fixed `module --terse avail` returned **2,086**
names; their union contained **2,087**, without traversal or command-output
truncation. Missing legacy paths, skipped symbolic links and unevaluated shell
conditions remain provisional/partial warnings. This check neither submitted a
compute job nor modified cluster files. The first normal worker refresh exposed
cold shared-filesystem latency and reached the original four-second enumeration
bound. A focused follow-up increased automatic enumeration to twenty seconds
and independently validates the complete candidate path list before optional
availability; truncated enumeration can no longer shorten that command's path.
Its 140 focused backend/module/SSH tests passed; root also ran 132 related
backend/module/async-Slurm checks. A subsequent ordinary worker refresh saved
**2,076** names without truncation, with 26 intended paths and 25 validated paths
(the absent legacy path stays visible). The portal then rendered the full saved
catalog and eleven collapsed partition groups. Command availability remains an
explicit per-cluster owner option rather than being enabled by that refresh.

Authenticated checks on the refreshed local portal confirmed collapsed groups
for both configured Slurm sites, the owner-only test preview, module-availability
option, directory browser and unfinished-cluster guidance in project settings.
The diagnostic preview created no project, key, job or reservation on either site.
These observations do not certify site timing bounds or module combinations on
compute nodes.

### Post-login cluster configuration, 22 October 2026

The final PostgreSQL suite passed **1,084 tests and 65 subtests**, with one
optional NumPy test skipped and two upstream LDAP dependency deprecation
warnings. Migration drift checks passed. New coverage includes complete
provisional module catalogs from trusted system path data, maximum single-node
resource defaults, explicit reviewed-job timing inputs, read-only bounded
directory metadata, human/worker ownership and CSRF, pending-configuration
admission fencing, suspension under authority locks, durable canonical receipts,
lost acknowledgements and account-freshness deferral without stopping core
accounting. Imports of the resource derivation helper are also tested with
Django/controller imports unavailable.

The separate `cb-post-connect-20261022` Docker fixture used the personal
connector overlay. The acceptance client paired without a manually started
worker, completed discovery, confirmed that agents could not use an unconfirmed
connection, browsed the remote account home, and saved a baseline directory
outside the original worker scope. The connector persisted the revised profile
before acknowledgement. A reviewed Python package then ran as native Slurm job
`1`, with expected stdout/artifact hash and final accounting of 1 CPU
core-second, zero GPUs and zero reservations. Restart reused the same identity
and selected directory; removal revoked the connector and closed its SSH master.

The first native run found a startup-revocation exit-status race after its job
had completed and settled. Startup revocation now ends gracefully and closes
the session, with a dedicated regression; the complete final native run passed.
These runs used synthetic credentials only, and the disposable services,
volumes and keys were removed. Existing portal PostgreSQL and mock LDAP were
retained. After backing up and migrating the running portal through `0014`, the
user restarted the existing connector with its saved state. Actual read-only
lab discovery then returned all **21 expected modules**, including eight Python
entries, from the site's canonical module directories. The catalog explicitly
remains provisional filename metadata. Maximum resource defaults appeared in
the saved cluster configuration, with automatic execution disabled.

A real authenticated LDAP-owner HTTP session opened the configuration page and
queued one directory request through that running lab connector. It returned
29 folders, writable access and no truncation. The configured working directory
remained unchanged and no compute job was submitted. This confirms live module
cataloging and folder browsing, not arbitrary module combinations or a submitted
job on that institutional cluster. No private key, pairing token, password or
directory-entry names were printed in acceptance output.

### Personal connector and configuration removal, 22 October 2026

The PostgreSQL suite passed **932 tests and 65 subtests**, with one optional
NumPy test skipped and two upstream LDAP dependency deprecation warnings.
Migration drift checks reported no changes. Coverage includes one-use hashed
pairing credentials, ownership and CSRF, stale setup revisions, PostgreSQL
discovery/re-pair locking, local session cleanup, key-picker cancellation and
headless fallback, and owner-confirmed cluster archival. GUI tests use mocked
dialogs; no actual desktop key-selection window or institutional key was used.

The separately named disposable Docker Slurm stack used `compose.test.yml`
and `compose.personal-test.yml`, with only the controller and Slurm fixture
started before the acceptance client. The client enrolled its own personal
worker, established a native OpenSSH master and completed discovery. A reviewed
Python job ran as native Slurm job `2`, produced the expected stdout and artifact
hash, and settled at 2 CPU core-seconds with zero outstanding reservations.
SIGTERM closed the connector-owned master, and a restart reused the saved worker
identity. Owner-confirmed removal then hid the connection from agent execution,
revoked the dedicated connector and closed its resumed SSH master. No remote
files were removed. The test containers, volumes and synthetic credentials were
removed afterward; persistent portal PostgreSQL and mock LDAP were retained.

CI defines the same personal connector acceptance job. These local results do
not claim a remote CI run, actual Cisco MFA acceptance or async support for the
optional environment, workflow and storage extensions. See
[personal connectors](personal-connectors.md) for the local setup and trust
boundaries.

The running development portal was backed up and migrated through `0012`.
At the user's request, its earlier lab configurations and disposable job
history were then purged, test compute balances reset, and the old lab worker
removed from startup. Accounts, projects, API keys and the synthetic demo
connection were retained. Authenticated HTTP checks confirmed the local-key
picker guidance, removal control and confirmation page. This development-data
cleanup made no SSH requests and did not delete HPC files.

An interactive development retry exposed a startup bug under umask `0002`:
`mkdir(parents=True)` created an intermediate state directory as `0775`, which
the existing SSH safety check rejected with an unhelpful generic message.
State setup now validates ancestors before key selection and creates every
missing level as `0700`. Existing unsafe directories are rejected with a fixed
actionable diagnostic and are never silently changed. All **87 focused tests**
passed. The updated Docker acceptance ran with umask `0002` and a nested state
path; pairing, discovery, native job `1`, output/hash validation, restart and
revocation cleanup passed. Accounting settled at 1 CPU core-second with zero
reservations. No institutional SSH login was attempted for this regression.

### PostgreSQL, LDAP and async Slurm acceptance, 22 October 2026

A full PostgreSQL regression run passed **780 tests and 65 subtests**, with one
optional NumPy check skipped. Independent clients exercised shared admission
balances, unique claims, cancellation settlement and concurrent withdrawal of
dispatch authority. Storage regression tests covered project/pool lock order in
reservation, publication and failed-publication cleanup. An existing development
SQLite installation was transferred offline and independently verified across
all 46 tables, including artifact bytes and accounting balances; its obsolete
SQLite configuration was fenced before resuming the PostgreSQL portal.

The native asynchronous core Slurm stack used `compose.test.yml` plus
`compose.async-test.yml`. The management smoke passed rejection, linked revision,
human approval, SSH submission, output/hash checks and settled native job `1`.
Accounting charged one CPU core-second and retained zero reservations. Only
this disposable stack and its synthetic volumes were removed afterward. No
institutional submission was made for these checks. Optional extension workers
remain synchronous; this result does not claim async extension support.

The independent [synthetic LDAP fixture](../deploy/ldap/README.md) passed native
LDAPS and mandatory StartTLS, wrong-CA/hostname rejection, two eligible accounts,
disabled/expired account rejection, local/directory name collision separation,
stale verification refresh and preserved operator suspension. No plaintext
directory passwords were persisted in application records. Native testing found
and fixed a strict TLS certificate-chain defect and the SDK's mapping type;
their regressions plus the final identity/user/dispatch checks passed **55
focused PostgreSQL tests**. Actual portal logins also accepted the two eligible
mock users without automatically granting projects or HPC connections.

The optional portal overlay needs its separate bridge to publish verified LDAPS
on loopback; the default LDAP acceptance remains on an internal-only network.
CI defines native LDAP, PostgreSQL and async Slurm jobs, but local acceptance
does not establish that remote CI has run. Protected OS identity separation,
institutional MFA, hostile workload isolation and shared-server production
acceptance remain separate gates.

### Cluster-first setup acceptance, 21 October 2026

The management smoke client now saves cluster details without a worker or
credential profile, opens the saved setup, loads an owned worker's current
profile fingerprint, and binds an approved directory in a separate request.
Both the native fake service and a fresh disposable Docker Slurm stack passed
this flow, including successful discovery and the existing project/key/review
checks. The fixture execution settled at one CPU core-second with zero
reservations. No institutional jobs or credentials were used.

Regression coverage includes an owner with no workers, new proposed directories,
ownership and CSRF, stale setup/profile revisions, concurrent duplicate binding,
withdrawn/revoked workers during discovery, and the first-discovery execution
gate. Worker registration tests cover hash-only storage, one-time responses,
replay and concurrent issuance. The production URL routes are exercised too.
The final integrated suite passed 630 tests and 64 subtests, with one optional
NumPy calibration skipped. Migration drift checks reported no changes.

The running local portal was backed up and migrated through broker migrations
0007 and 0008. Authenticated checks confirmed that existing connection IDs,
execution revisions and project/job/attempt counts were preserved. This upgrade
does not establish or start a new worker automatically; protected worker
configuration remains a separate operator step.

### Optional extension acceptance, 21 October 2026

The integrated extension branch was checked with Docker Engine 29.9.2 and
Compose 5.6.1 using a separately named disposable Slurm stack. The default
configuration and each optional app were exercised independently. No
institutional credentials or lab jobs were used for these checks.

| Check | Observed result |
| --- | --- |
| Core-only management and execution | Public project/key/cluster forms, rejection, linked revision and native execution passed. The completed job spent 2 CPU core-seconds and left zero reservations. |
| Environment build and reuse | An empty stdlib venv was built as a reviewed compute job, published only after final accounting and metadata validation, explicitly permitted by a human, and used by a second reviewed job. Both jobs settled: 2 CPU core-seconds total, 2 lifetime slots, zero reservations. Exact prefix and output hash checks passed; the immutable recipe cache reused the original build. |
| Three-stage workflow | `produce → double → sum` ran as three individually reviewed native jobs. Exact bytes, hashes, producer attempt/artifact IDs and consumer package descriptors matched. Repeated advances created no duplicates. Accounting settled at 3 CPU core-seconds, 3 lifetime slots and zero reservations. |
| Read-only storage probe | Explicit owner enablement produced fresh `statvfs` byte/inode counts over fixture SSH. Native quota and unpinned filesystem identity remained unknown; planning correctly returned `unknown`. No job or compute charge was admitted, no directory scan/write was requested, and the client paused probes afterward. |
| Core-only fresh processes | Clean migrations created no optional tables, apps or routes; ordinary MCP upload/preparation/admission/fake settlement passed. Worker and MCP startup also passed with controller and optional-app imports denied. |

One acceptance-client bug was found: an expected HTTP 403 from a human-only
promotion endpoint returned HTML, while the client tried to parse JSON. The
client was fixed and the complete environment check rerun successfully. Its
first build had already completed and settled; no ambiguous submission was
retried. Final native scheduler inspection showed all seven fixture allocations
completed, including this initial build and the separate core test. Jobs with
zero whole-second elapsed time were conservatively charged one CPU core-second
per allocated CPU, as documented by the broker's meter.

The integrated suite passed **582 tests and 64 subtests**, with one optional
NumPy calibration skipped in the main environment. The isolated science-kit
checks also ran with NumPy 2.3.4; only tiny matrices were allocated. Migration
drift checks and all planning schemas/examples passed. Actual fresh model chats
remain a separate manual acceptance step; an official MCP SDK connection check
does not substitute for that step.

The optional Compose overlays are `compose.environments-test.yml`,
`compose.workflows-test.yml`, and `compose.storage-test.yml` in `deploy/`.
Each enables only its selected app for control and worker, mounts only its
fixture acceptance client read-only, and requires explicit synthetic metadata.
CI now defines a separate matrix for these three checks. See
[environment acceptance](environment-builds.md),
[workflow acceptance](workflow-acceptance.md), and
[storage acceptance](extensions-storage.md). These local results do not imply
that remote CI has run or that a production installation is accepted.

Live verification completed on **21 October 2026**, with Docker Engine 29.9.2 and Compose 5.6.1. After refreshing group access, both stacks were rebuilt from the final application source, including key confirmation modes, lifetime admission limits, protected worker profiles, and discovery refresh guards. Both passed the expanded `--management` HTTP workflow. The fixture used Debian Slurm 22.05.8, MariaDB 10.11.19, and OpenSSH 9.2p1 (Debian `2+deb12u10`) packages. Both Compose documents also validated against the [official Compose specification schema](https://github.com/compose-spec/compose-spec/blob/main/schema/compose-spec.json) and the installed Compose plugin.

| Final clean run | Observed result |
| --- | --- |
| Fake stack | Portal cluster enrollment/discovery, project creation, shared key funds, one-time reveal, rotation/revocation, and key access changes passed. A review key on a budgeted project followed rejection → linked revision → human approval → simulated results. Accounting settled at 1 CPU core-second spent, zero GPU usage and zero outstanding reservations. Uploaded code was explicitly not executed. |
| Slurm stack | The same complete portal/review workflow executed the uploaded Python on native job `1`; expected stdout marker and JSON artifact/hash matched. `sacct` reported `COMPLETED`, exit `0:0`, 1 allocated CPU, elapsed/accounted 1 second, and 128 MiB allocated memory. Broker accounting settled at 1 CPU core-second, zero GPU usage, zero reservations. |
| Execution limits and attribution | Both stacks preserved the job name, project/owner/creating-key attribution, and derived Completed/Rejected states. After editing the CPU total to 10,800 seconds, spent remained 1 and available was 10,799. The lifetime count remained 1 admitted of 1, with 0 active and 0 remaining. A newly created automatic key received `TOTAL_JOB_LIMIT`; it created no second execution/reservation. |
| Running container mounts/identities | Control and worker ran as UID/GID10001. Worker journal mode was `0700`; worker loaded protected credential profiles and had the fixture SSH key, with no control database/owner metadata. Control had its database and no SSH identity. Worker config and identity mounts were read-only. Neither contained a Docker socket. |

Live testing caught and corrected two deployment issues: private journal directories initially had mode `0755`, which the worker correctly rejected; and Slurm22 initially attempted cgroup/v2/systemd initialization in the container. These were corrected with private image/volume directories and the fixture's explicit legacy plugin configuration. The optional PMIx runtime library was also added from Debian packages. Test containers, volumes, networks, and synthetic credentials were removed after verification; built images were retained.

Static checks run without Docker or credentials:

```sh
python -m unittest discover -s tests/system -v
```

All 15 static/client checks passed. They validate isolation intent in Compose, source/runtime secret handling, required fixture components, shell syntax, form serialization, and response/redirect handling. They do not execute Docker, parse configuration with installed Slurm daemons, inspect Linux permissions at runtime, prove successful image builds, or run the application workflow. A missing Docker engine must be reported as an unperformed live-container test, never replaced by a claim that these static tests prove the cluster works.

The targeted Django management suite also passed all 77 cases against a disposable test database:

```sh
python -m pytest tests/unit/test_management.py -q
```

It covers human sessions/CSRF, owner/project/key isolation, hash-only key storage and one-time responses, shared balances, rotation atomicity, per-key execution review, historical admission limits, state/attribution rendering, discovery lease fencing and busy-profile updates, snapshot escaping, and immutable existing-environment recipes with canonical module paths. These tests never read institutional SSH credentials or call a cluster. The native synthetic HTTP workflow also passed before the final container runs; it is useful for faster iteration but does not replace container/Slurm acceptance.

Both Compose smoke services enable `--management`. The client saves a cluster first, then binds the worker's advertised credential-profile alias, current fingerprint and allowed public target in a separate request. It waits for discovery and checks that its software snapshot describes the login node without claiming compute-node verification. It creates a budgeted project with explicit limits and a lifetime limit of one execution, then creates two independent keys sharing one allowance. The keys require review for each execution, demonstrating that a key can require approval on a budgeted project. The client verifies duplicate key-creation requests do not reveal a key again, rotates and revokes keys, and checks deny-all versus inherited cluster access. Full keys stay in the smoke process's memory; they are not printed in test results.

The client then uses the newly created project and rotated key for denied agent approval, human rejection/comment retrieval, a revised human-approved package, completed execution and settled accounting, log/output hashes, charged CPU usage, and zero outstanding compute reservations. It checks that job states show rejection/completion even though the immutable package remains prepared, and that the API and project job list retain the agent-supplied name, owner, and creating-key attribution. A later allowance edit must preserve spent funds and the durable lifetime count. Finally, a new automatic key must receive `TOTAL_JOB_LIMIT` when requesting another execution of the completed package, without creating a new reservation or consuming compute. The rejected human decision consumes no lifetime slot; the accepted execution consumes one permanently.

Fake output is explicitly simulated; only the Slurm branch requires the actual Python output marker and JSON result. The newly discovered cluster is checked separately; the job uses the demo's original cluster and its bound environment recipe. Run against clean fixture volumes without other concurrent jobs. The smoke project deliberately uses a limit of one to exercise exhaustion; the ordinary project form's default is 1,000 executions.

Live acceptance must record image/package versions, Compose validation, build/startup results, the smoke result, native Slurm job IDs, terminal accounting, and cleanup. Test a subsequent worker restart using its journal, a failed job, and cancellation before advertising these recovery paths as supported. University MFA, GPUs, site-specific allocation rounding, and hostile shared-UID workloads need their separate acceptance gates in [the testing plan](testing.md).

Images currently use explicit distro/Python version tags and install application dependencies constrained by `requirements.lock`; Debian packages and Python build tooling still resolve at build time. They are development recipes; release builds still require reviewed immutable base digests, stronger build reproducibility, SBOM/provenance, and vulnerability/license review. Slurm is installed from [Debian's maintained packages](https://packages.debian.org/bookworm/slurm-wlm), rather than an external prebuilt HPC image. Fixture options were checked against the [Debian bookworm Slurm manual](https://manpages.debian.org/bookworm/slurm-client/slurm.conf.5.en.html) and [SlurmDBD manual](https://manpages.debian.org/bookworm/slurmdbd/slurmdbd.conf.5.en.html), then parsed/executed by the actual daemons. Site deployments must use their installed version's configuration/accounting semantics.
