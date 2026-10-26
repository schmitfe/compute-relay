# Dispatch performance and next transport steps

Measured 2026-10-26 at commit `5066761` against the disposable local Docker Slurm
fixture. These numbers describe this host and fixture, not university-cluster
network latency or a production capacity guarantee. Each of four phases admitted
24 separate Python jobs, each sleeping 0.05 seconds and writing a tiny artifact.
The fixture has two scheduler CPUs. All jobs received human approval before the
connector started. Its startup, including authentication with the synthetic key,
is included in the dispatch measurement. Preparation and approval are excluded.

| Command slots | Time until all 24 native IDs | Native IDs per second | Time until all accounting settled |
| --- | --- | --- | --- |
| 1, first phase | 5.422 s | 4.426 | 33.560 s |
| 4, first phase | 3.083 s | 7.784 | 33.356 s |
| 4, second phase | 3.034 s | 7.910 | 32.512 s |
| 1, second phase | 5.579 s | 4.302 | 31.530 s |

The four-slot configuration dispatched about 1.8 times as fast in these runs.
It did not materially improve the time to settle all jobs. Scheduler queuing,
job launch/termination, accounting availability and Relay polling all contribute
to that second measurement; this experiment does not separate their costs.

Median remote submit calls took 54–56 ms, observations 208–211 ms, and collection
33–34 ms. Each phase made 58–80 observations for its 24 jobs. Every observation
starts a fresh helper and invokes both `squeue` and `sacct`. Each phase sent
2.6–3.1 MB of fixed helper source across its remote calls. The measurements
include passive wrappers around helper calls and controller HTTP requests.

## Reproduce

Use an isolated Compose project and unused local port. Only synthetic fixture
credentials are accepted by the benchmark. It starts and stops its own connector;
do not start the separate `worker` service in this stack.

```bash
export BROKER_TEST_PORT=18873
export COMPOSE_PROJECT_NAME=relay-benchmark
export COMPOSE_FILE=deploy/compose.test.yml:deploy/compose.benchmark.yml
docker compose build control slurm dispatch-benchmark
docker compose up -d --wait control slurm
docker compose run --rm --no-deps dispatch-benchmark
docker compose down --volumes --remove-orphans
```

Override the workload with `dispatch-benchmark --jobs 48 --parallelism 1 4`.
The final JSON contains operation counts, medians, p95 durations, transmitted
helper-source bytes and HTTP request counts. Runtime instrumentation is confined
to the benchmark child; production code is not patched or replaced.

## Recommended next changes

Normal personal connectors already reuse an authenticated OpenSSH master. Extra
command slots are multiplexed channels over that master, not extra MFA logins.
Each channel still starts a shell/Python process and retransmits the fixed helper.

1. Batch observations for a bounded set of attempts on the same connection.
   Validate each journal/native identity independently, then query `squeue` and
   `sacct` once for the group. Keep per-attempt leases, receipts and accounting.
   The current observation count makes this a stronger candidate than merely
   increasing SSH connection counts. Its actual benefit remains unmeasured.
2. Add a session-bound helper as an optional transport after measurement. Start
   the fixed, versioned helper once over a long-lived SSH channel; exchange
   bounded, correlated request/response frames. No listening port or separate
   cluster service is needed. Retain the one-shot transport for sites that
   restrict persistent login-node processes or unreliable long-lived channels.
3. Keep authority local: validate current approval/budget immediately before
   dispatch, fsync intent before transmitting a submit, and treat a lost response
   as uncertain. A remote queue may contain already-authorized work only. It
   must never choose new jobs or blindly replay `sbatch` after reconnecting.

Batching status and keeping the helper alive address different costs and can be
combined. Neither changes the scientific granularity of jobs. Very short,
homogeneous tasks may additionally benefit from explicit Slurm arrays or a
reviewed allocation that executes a bounded task bundle; those are separate
execution features, not an implicit merger of approved jobs.
