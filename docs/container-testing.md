# Container development and disposable Slurm testing

The container stacks exercise the application on a workstation without contacting an institutional cluster. `deploy/compose.fake.yml` runs the broker and worker with the fake backend; `deploy/compose.test.yml` adds a real, disposable CPU Slurm cluster over SSH. These are development/acceptance fixtures, not a production deployment or proof of host-level credential isolation.

The base Compose fixtures use JSON syntax, which is valid YAML; additional test
overlays use YAML. Docker Compose must validate and execute the combined
configuration on a host with a working container engine. No container engine is
installed by these scripts or tests.

## Layout and identity boundaries

| Component | Image and identity | Storage/authority |
| --- | --- | --- |
| Control | `deploy/Dockerfile`, `control` target, UID/GID 10001 | Own SQLite/artifact volume; owner/project demo metadata; worker enrollment metadata. No SSH-key volume. |
| Worker | Same Dockerfile, `worker` target, UID/GID 10001 | Read-only worker config/token; separate durable journal; fixture-only SSH identity. No control database or human credential mount. |
| Smoke client | Same Dockerfile, `smoke` target, UID/GID 10001 | Read-only synthetic demo metadata; invokes job APIs and human review endpoints. No SSH identity or database mount. |
| Slurm fixture | `deploy/slurm/Dockerfile`, root for scheduler/SSH initialization | MariaDB, Munge, SlurmDBD, controller, compute daemon, and SSH server in one disposable container. Separate `brokerjob` remote UID runs batch jobs. |

The stack publishes only `127.0.0.1:8765` for the control UI/API by default. Set `BROKER_TEST_PORT` to select another loopback port; internal API ports stay unchanged. SSH, database, Munge, and scheduler ports are internal. The internal Compose network has no default outbound Internet path; builds can download declared Python/Debian dependencies. No service mounts a host Docker socket, a home directory, an existing SSH agent, or institutional keys.

The fixture creates fresh SSH/Munge/database secrets at runtime. The worker-only key volume contains an SSH key and the fixture's directly generated host-key entry. This is fixture provisioning, not trust-on-first-use against a real server. Dockerfile-specific ignore files allowlist the build inputs instead of sending the whole workspace to the builder. The control bootstrap copies only `worker.json` and `worker-token` to the worker-config volume; owner credentials stay in the separate demo-metadata volume. The worker obtains leases/grants and reports through the control API, rather than connecting to its database.

Application services use a read-only root filesystem, private `0700` named data/journal volumes and bounded temporary filesystems, dropped Linux capabilities, and no-new-privileges. The Slurm fixture is intentionally less restrictive to run ordinary distro daemons; it does not request privileged mode or host cgroup mounts. It uses `task/none`, `proctrack/linuxproc`, and an explicit legacy cgroup plugin selection for startup, avoiding Slurm22's automatic cgroup/v2 systemd probe inside the container. No task/proctrack cgroup enforcement is configured. It schedules trusted tiny test jobs and does not demonstrate hostile workload containment or GPU isolation.

An agent that controls the host Docker daemon can inspect these fixture secrets. The same-account local package and these fixtures do not supply an operating-system secret boundary; see [security](security.md). Ordinary development bootstrap is explicitly synthetic and must not enroll real credentials.

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

The fixture cluster is `broker-test`, host `slurm-fixture`, partition `debug`, account `broker`, remote user `brokerjob`, and work root `/home/brokerjob/jobs`. It advertises two CPU cores, 1,024 MiB memory, no GPUs, and a maximum ten-minute partition limit. `JobRequeue=0`, `OverTimeLimit=0`, and `KillWait=5` make its test configuration explicit. Bootstrap uses small whole-minute jobs. Broker reservations use reviewed effective allocation times requested walltime; these fixture settings are not evidence about another site's scheduling behavior.

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

## Native helper-upgrade acceptance

The upgrade overlay exercises a separate native adapter/journal boundary using
the generated fixture key and pinned host key. It accepts only the dedicated
fixture target and does not use an institutional account. Use a distinct
disposable project name and the same loopback-port override throughout:

```sh
docker compose -p compute-relay-upgrade-test \
  -f deploy/compose.test.yml -f deploy/compose.upgrade-test.yml \
  build slurm control upgrade-smoke
docker compose -p compute-relay-upgrade-test \
  -f deploy/compose.test.yml -f deploy/compose.upgrade-test.yml \
  up -d --wait slurm control
docker compose -p compute-relay-upgrade-test \
  -f deploy/compose.test.yml -f deploy/compose.upgrade-test.yml \
  run --rm --no-deps upgrade-smoke
docker compose -p compute-relay-upgrade-test \
  -f deploy/compose.test.yml -f deploy/compose.upgrade-test.yml \
  down --volumes
```

`tools/helper_upgrade_smoke.py` prepares and submits two bounded stdlib jobs with
a reviewed comment-only difference in the installed execution bundle, then
uses the current bundle to reconcile, cancel, observe and collect. One accepted
submission reply is deliberately lost. The check requires final native
accounting, stable recovered identity, refusal to submit an old package under
the new execution hash, and exactly two submission calls. It never retrieves or
executes helper source from a saved manifest or journal. Each job requests one
CPU and one minute; the tool cancels the jobs and attempts cancellation again
during cleanup.

This tool calls the native adapter directly. It does not claim HTTP approval,
project-budget admission or broker-ledger settlement; the ordinary and personal
connector smoke flows provide that separate evidence. The overlay's presence
and passing unit tests do not establish that a native Docker run passed against
the final integrated source. Keep the actual run result with its commit or CI
record before making that claim.

## Current acceptance scope

This is an optional developer fixture. The normal package uses SQLite and runs
without Docker. The manual Slurm CI workflow exercises pairing, reviewed
submission, outputs, accounting and cleanup using only synthetic credentials.
The fixture's MariaDB belongs to Slurm accounting; it is not a separate broker
database requirement.

Run the default test suite and `tools/package_smoke.py`; run this fixture
explicitly when native Slurm acceptance is required.

The SQLite tests additionally cover input provenance, upload-crash recovery,
logical metadata limits/maintenance, management priority, lease renewal and
durable report replay. These controlled faults complement the native fixture;
they do not require filling a real disk or interrupting a real cluster.
