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

## Current acceptance scope

This is an optional developer fixture. The normal package uses SQLite and runs
without Docker. The manual Slurm CI workflow exercises pairing, reviewed
submission, outputs, accounting and cleanup using only synthetic credentials.
The fixture's MariaDB belongs to Slurm accounting; it is not a separate broker
database requirement.

Historical acceptance logs for the former PostgreSQL/LDAP/shared-server design
are preserved on `codex/archive-multi-user-server` at `520effc`. Their old counts
are not evidence that the new single-user package passed current acceptance.
Run the default SQLite suite and `tools/package_smoke.py` for the local package,
and run this fixture explicitly when native Slurm acceptance is required.
