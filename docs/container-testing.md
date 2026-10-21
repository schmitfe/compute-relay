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

Both Compose smoke services enable `--management`. The client enrolls a cluster using the worker's advertised credential-profile alias and allowed public target, waits for discovery, and checks that its software snapshot describes the login node without claiming compute-node verification. It creates a budgeted project with explicit limits and a lifetime limit of one execution, then creates two independent keys sharing one allowance. The keys require review for each execution, demonstrating that a key can require approval on a budgeted project. The client verifies duplicate key-creation requests do not reveal a key again, rotates and revokes keys, and checks deny-all versus inherited cluster access. Full keys stay in the smoke process's memory; they are not printed in test results.

The client then uses the newly created project and rotated key for denied agent approval, human rejection/comment retrieval, a revised human-approved package, completed execution and settled accounting, log/output hashes, charged CPU usage, and zero outstanding compute reservations. It checks that job states show rejection/completion even though the immutable package remains prepared, and that the API and project job list retain the agent-supplied name, owner, and creating-key attribution. A later allowance edit must preserve spent funds and the durable lifetime count. Finally, a new automatic key must receive `TOTAL_JOB_LIMIT` when requesting another execution of the completed package, without creating a new reservation or consuming compute. The rejected human decision consumes no lifetime slot; the accepted execution consumes one permanently.

Fake output is explicitly simulated; only the Slurm branch requires the actual Python output marker and JSON result. The newly discovered cluster is checked separately; the job uses the demo's original cluster and its bound environment recipe. Run against clean fixture volumes without other concurrent jobs. The smoke project deliberately uses a limit of one to exercise exhaustion; the ordinary project form's default is 1,000 executions.

Live acceptance must record image/package versions, Compose validation, build/startup results, the smoke result, native Slurm job IDs, terminal accounting, and cleanup. Test a subsequent worker restart using its journal, a failed job, and cancellation before advertising these recovery paths as supported. University MFA, GPUs, site-specific allocation rounding, and hostile shared-UID workloads need their separate acceptance gates in [the testing plan](testing.md).

Images currently use explicit distro/Python version tags and install application dependencies constrained by `requirements.lock`; Debian packages and Python build tooling still resolve at build time. They are development recipes; release builds still require reviewed immutable base digests, stronger build reproducibility, SBOM/provenance, and vulnerability/license review. Slurm is installed from [Debian's maintained packages](https://packages.debian.org/bookworm/slurm-wlm), rather than an external prebuilt HPC image. Fixture options were checked against the [Debian bookworm Slurm manual](https://manpages.debian.org/bookworm/slurm-client/slurm.conf.5.en.html) and [SlurmDBD manual](https://manpages.debian.org/bookworm/slurmdbd/slurmdbd.conf.5.en.html), then parsed/executed by the actual daemons. Site deployments must use their installed version's configuration/accounting semantics.
