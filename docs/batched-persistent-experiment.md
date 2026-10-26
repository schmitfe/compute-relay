# Batched status and persistent helper experiment

This work lives on `codex/batched-persistent-helper`. Both switches default off.
The stable `main` branch remains the comparison baseline. No database migration,
cluster daemon installation, listening port or new agent tool is required.

## Enable explicitly

Use the same experimental package revision for the controller and connectors.
Set the flags in the terminal that starts the connector or local launcher:

```bash
export COMPUTE_RELAY_EXPERIMENTAL_STATUS_BATCH_SIZE=32
export COMPUTE_RELAY_EXPERIMENTAL_PERSISTENT_HELPER=1
compute-relay
```

Batch sizes range from 1 through 64. The existing
`COMPUTE_RELAY_MAX_REMOTE_OPERATIONS` still bounds concurrent command slots
(default four, maximum eight). Setting batch size to one and the persistent
switch to zero selects the existing path. There is no automatic resubmission
through another transport after an uncertain operation.

## Batching

The controller first picks the oldest due work using the normal dispatcher.
Only an observation may grow into a batch. Subsequent eligible observations
must be consecutive in that ordering and belong to the same connection. An older
submission, cancellation, reconciliation or another connection ends the batch.
Additional items have a conservative combined payload bound of 8 MiB including
the first item. An individually large first package retains the existing
single-operation bound and receives no additional items.

Each item has its own attempt ID, generation, expiring lease, heartbeat, package
verification, journal lock and report receipt. Grouping does not combine approval,
budgets, native identities or accounting. Worker polls exclude all locally
in-progress attempts. Configuration changes wait for those operations to drain.

The fixed remote batch helper verifies every native identity against its own
immutable remote journal. A bad journal binding becomes an unknown result for
that item; it does not authorize another identity or contaminate another result.
One `squeue` query covers the group. One `sacct` query covers only IDs absent from
the queue. Results are split by job ID rather than repeating the whole output for
each attempt. Existing parsers still check markers, submit times, work directories
and final allocation accounting independently. Missing or ambiguous rows remain
unknown and retain their reservations.

Terminal collection and extension hooks reuse the existing path. Collection
within a batch is sequential, preserving the command-slot bound and avoiding
hundreds of simultaneous transfers. Each completed item's heartbeat stops before
its final report; slower later items cannot renew an already settled lease.

## Persistent helper

The connector owns a pool of at most its command-slot count of SSH channels.
Each channel launches one fixed stdlib Python program through the existing
authenticated OpenSSH master and serves sequential requests on stdin/stdout.
Several channels provide bounded parallelism; remote jobs themselves do not
consume helper channels after submission.

Messages use a four-byte length, a versioned JSON envelope and a correlated
request ID. Request, response and diagnostic sizes are bounded. A channel binds
to one protected configuration; it cannot switch roots halfway through a stream.
Only installed, allowlisted helper source is accepted. The original execution
bundle and its prepared-package hash are unchanged; the streaming dispatcher
calls those same execution components and adds the fixed framing/batch layer.
Discovery and environment inspection retain the one-shot path.

The pool has a finite pending-operation bound and never authenticates itself.
Its SSH arguments require the connector's validated master and fail closed when
that master is absent. Timeout, malformed reply, EOF or cancellation closes and
reaps the affected channel. A failed exchange is never replayed by the pool.
Durable submit intent still requires reconciliation. Idle dead channels may be
replaced for a subsequent operation. Configuration changes retire idle helpers
before starting helpers with the new configuration.

All helper channels drain/close before SSH-session recovery. The existing network
backoff, credential rejection stop, authentication limits and original session
deadline remain in force. Helpers have no listener and exit when their SSH stdio
closes. Sites that prohibit persistent login-node processes can leave this
feature disabled and use batching independently.

## Verification and comparison

Tests cover 200 and 500 journal-bound statuses, 500 independently settled
controller attempts, worker batches, queue fairness, malformed/truncated frames,
source/configuration pinning, cancellation during process creation, lost submit
replies and heartbeat shutdown during slow collection. A synthetic mixed batch
of 500 statuses needs sixteen scheduler queries rather than one thousand.

The disposable benchmark supports `standard`, `batched`, `persistent` and
`combined` modes. It reports dispatch and settlement time, per-operation latency,
status query counts, helper source bytes, helper starts, Linux worker RSS high
water and aggregate persistent-helper RSS high water. Memory figures exclude the
controller, SSH master and Slurm daemons. Helper memory is reported by Linux
`getrusage`; worker memory comes from `/proc`. No timing assertion is baked into
unit tests.

```bash
export BROKER_TEST_PORT=18875
export COMPOSE_PROJECT_NAME=relay-batch-experiment
export COMPOSE_FILE=deploy/compose.test.yml:deploy/compose.benchmark.yml
docker compose build control slurm dispatch-benchmark
docker compose up -d --wait control slurm
docker compose run --name relay-batch-results --no-deps dispatch-benchmark \
  --jobs 200 --parallelism 4 \
  --modes standard batched persistent combined --batch-size 32 --timeout 900
docker cp relay-batch-results:/results/dispatch-benchmark.json ./dispatch-benchmark.json
docker compose down --volumes --remove-orphans
```

The fixture has two scheduler CPUs and runs trusted 0.05-second Python jobs.
These are control-path measurements, not scientific-compute benchmarks or a
production-cluster capacity guarantee. Repeat/reverse mode order when assessing
small differences. Results are saved atomically after each phase; `ok: true`
marks a completed run. Copy the file before deleting the results volume. For a
separate interruption trial, use `--modes combined --interrupt-master`. That
switch targets only the disposable fixture's known SSH master.

## Recorded results: 26 October 2026

The [aggregate measurements](benchmarks/2026-10-26-batched-persistent.json)
include 1,200 native Slurm jobs across six 200-job cohorts. Independent checks of
the controller database and Slurm accounting confirmed every job succeeded and
settled, with no additional native job IDs. All four modes were exercised in the
initial run. The first three modes' measurements were retained; the initial
combined measurements were not retained, so that mode was repeated with saved
JSON output. A further combined cohort tested SSH interruption and recovery.

| Mode | All scheduler IDs received | All accounting settled | Scheduler status queries | Helper source transferred | Worker peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| Standard, preliminary | 32.4 s | 280.0 s | 2,012 | 34.30 MB | 43.9 MiB |
| Batched, preliminary | 26.4 s | 290.8 s | 311 | 15.08 MB | 45.4 MiB |
| Persistent, preliminary | 29.6 s | 274.1 s | 2,070 | 0.118 MB | 43.8 MiB |
| Combined, final revision | 40.6 s | 277.4 s | 397 | 0.090 MB | 46.3 MiB |

The preliminary measurements preceded the final framing, cancellation and lease
cleanup fixes. The final combined run and interruption trial used implementation
`f17d73a`. Each row is one trial, with fixed initial mode ordering and uncontrolled
host load. These timings do not establish a causal speed improvement or
regression. Adaptive polling also changes how many statuses are observed in each
trial: query counts measure actual traffic, not an equal number of observations.

The useful result is lower scheduler and transport overhead: batching alone
reduced status queries by about 85% in the preliminary comparison. Persistent
helpers removed more than 99% of helper-source transfers. The final combined
trial served 1,039 status items with 397 scheduler queries and three helper
process starts. Its aggregate helper RSS high-water estimate was 50.1 MiB,
separate from the worker RSS above. Total settlement time stayed near the
baseline in this fixture.

During the interruption trial, the known SSH master was stopped after all 200
native IDs were confirmed. The connector recovered using the existing bounded
reconnect policy; all 200 IDs remained unchanged, all jobs settled, and Slurm
accounting contained exactly 200 successful jobs. Six helper processes started
over the complete trial as the pool was rebuilt, while simultaneous channels
remained bounded by four slots. Lost-submit-response tests separately verify
reconciliation without submitting a second job.

Full suites passed on Python 3.12 and 3.13: **2,187 tests, one skipped and 67
subtests on each version**. Clean wheel/sdist installation, dependency checking,
portal startup, HTML rendering, shutdown and same-state restart also passed.
An additional focused stream test exercises batched observation through a reused
helper process. The synthetic 500-item tests verify query bounds, independently
settled budgets and queue fairness without relying on wall-clock assertions.

## Promotion criteria and remaining work

Both options remain off by default, and `main` is unchanged. The tests cover
hundreds of admitted jobs; a two-CPU fixture does not establish behavior with
hundreds of simultaneously executing scientific allocations. Before promotion,
repeat the comparison on a permitted cluster, vary latency, artifact sizes and
mode order, and include longer runs with hundreds of active jobs.

Per-attempt HTTP leases, heartbeats, reports, package verification and artifact
collection still cost time. The combined trial made 735 lease requests and
1,239 report requests for 200 jobs. Lower scheduler traffic therefore does not
remove every controller bottleneck. Collection within each status batch remains
sequential to bound transfer fan-out; large result sets need separate measurement.
The implementation preserves independent job authorization, accounting and
recovery rather than treating a batch as one execution.
