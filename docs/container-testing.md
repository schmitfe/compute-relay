# Disposable Slurm testing

Developer fixtures: `deploy/compose.fake.yml` simulates jobs;
`deploy/compose.test.yml` adds CPU Slurm over SSH. Use synthetic credentials only.

## Layout and identity boundaries

| Service | Authority |
| --- | --- |
| Control | UID/GID 10001, SQLite/artifacts and owner/demo metadata; no SSH key |
| Worker | UID/GID 10001, read-only worker config/token, private journal/fixture SSH key; no control DB |
| Smoke | UID/GID 10001, synthetic demo credentials for HTTP review/tests; no SSH/DB mount |
| Slurm | Root starts MariaDB/Munge/Slurm/SSH; `brokerjob` runs jobs |

Only control publishes a host port, default `127.0.0.1:8765`; override with
`BROKER_TEST_PORT`. Other ports stay internal. Runtime networking has no default
Internet route; builds may fetch declared dependencies. No host Docker socket,
home, SSH agent or institutional key is mounted. Build contexts are allowlisted.
Fresh SSH/Munge/DB secrets and a fixture-generated pinned host key are synthetic;
only worker credentials enter its config volume, owner credentials stay separate.

App services use read-only roots, private `0700` data/journals, bounded temporary
filesystems, dropped capabilities and no-new-privileges. Slurm's ordinary daemons
need a less restricted fixture, without privileged mode/host cgroups. `task/none`,
`proctrack/linuxproc` and legacy cgroup startup provide no job cgroup or GPU
isolation. Docker administrators can inspect credentials. This is a trusted tiny
workload fixture, not a production security boundary.

## Build and run

Use Docker Engine/Compose v2, approximately 2 CPUs/2 GiB plus build headroom,
and a clean checkout. Source is built into images, not mounted editable.

The current session needs Docker access; refresh login after changing group
membership, never make its socket world-writable. Keep one port for the whole run:

```sh
export BROKER_TEST_PORT=8767
```

First run the fake stack:

```sh
docker compose -f deploy/compose.fake.yml --profile test build
docker compose -f deploy/compose.fake.yml up -d --wait control worker
docker compose -f deploy/compose.fake.yml --profile test run --rm smoke
```

Open the configured loopback port. Synthetic `/run/demo/demo.json` credentials
are read by smoke clients; do not copy them into reports/logs.

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

Fixture identity: cluster `broker-test`, host `slurm-fixture`, partition `debug`,
account `broker`, user `brokerjob`, root `/home/brokerjob/jobs`. Capacity is two
CPUs, 1,024 MiB, zero GPUs, ten-minute limit. `JobRequeue=0`, `OverTimeLimit=0`,
`KillWait=5` are fixture settings, not evidence about a real site.

Use the fixture's actual scheduler/accounting tools for diagnostics:

```sh
docker compose -f deploy/compose.test.yml exec slurm sinfo
docker compose -f deploy/compose.test.yml exec slurm squeue
docker compose -f deploy/compose.test.yml exec slurm sacct --starttime today --format=JobID,State,ExitCode,AllocCPUS,ElapsedRaw
docker compose -f deploy/compose.test.yml logs --tail 100 slurm control worker
```

Critical daemon exits fail the fixture. Health requires controller/node/accounting/
SSH readiness; actual job success still needs smoke output/accounting checks.

Remove only this disposable stack's resources when finished:

```sh
docker compose -f deploy/compose.test.yml down --volumes
```

`--volumes` deletes synthetic history/keys/journals. Never reuse fixture names or
volumes for real work. Recreating Slurm with outstanding jobs is a reconciliation
fault test; recreate the whole disposable stack for a clean run.

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

`tools/helper_upgrade_smoke.py` submits exactly two one-CPU/one-minute jobs under
reviewed helper hashes, loses one accepted reply, then reconciles/cancels/collects
through current code. It requires stable identities, final accounting and refusal
to submit an old package; stored helper source is never executed. It exercises
the adapter directly, not HTTP approval or broker settlement. Keep the actual
run result/commit before claiming native acceptance.

## Current acceptance scope

The normal package uses SQLite without Docker; fixture MariaDB serves Slurm
accounting only. Slurm CI runs on main/manual dispatch with synthetic credentials.
Run default tests and `tools/package_smoke.py` separately.

The personal-connector overlay checks 60-second owner-permitted renewal, eight
parallel submissions, master death, accepted-report reply loss, restart and
interrupted multichunk collection. It verifies unchanged identities, hashes,
settlement, revocation and same-state resume while HTTP/status waits run through
four Waitress threads. Ordinary probes timeout after three seconds; the latency
summary includes deliberate waits.

Fault injection exists only in a child launcher restricted to
`brokerjob@slurm-fixture`, never an application endpoint. On failure CI captures
`SIGUSR1` thread stacks without locals before bounded logs/state cleanup. These
checks neither explain the uncaptured portal stall nor certify institutional
VPN/MFA. Module-specific fixture commands live in their respective guides.
