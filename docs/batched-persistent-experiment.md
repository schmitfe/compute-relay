# Batched status and persistent helper experiment

The optional batching and persistent-helper implementation is included here for
evaluation; both switches default off. Default settings retain the standard path
as the comparison baseline. No database migration, cluster daemon installation,
listening port or new agent tool is required.

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

## Recorded results: 28 October 2026, real Slurm site

The acceptance run used the installed `f0dc61d` transport implementation against
a shared four-node cluster. Every job requested one CPU, 1 GiB and a one-minute
limit, with no GPUs. The site's allocation quantum charged two logical CPUs per
running job. Cohorts used `gpu`, `smp` and `mpi`; none used `interactive`.
At most 52 benchmark jobs were admitted concurrently, with four remote command
slots. Other users' load was not controlled.

The [sanitized aggregate measurements](benchmarks/2026-10-28-real-slurm.json)
retain cohort results and the separate MCP checks. Independent Slurm accounting
verified **944 unique native jobs: 940 completed
and four deliberately cancelled**, with no additional native IDs and CPU charges
matching the controller. These consumed 10.465 CPU core-hours. Another 52
admissions were cancelled before dispatch during an interrupted test and have
no native jobs or compute charge. The controller recorded 996 admissions in
total, including diagnostic cohorts. All successful jobs' result markers and
declared artifact hashes were checked; representative payload bytes were also
downloaded and hashed. Four successful jobs needed explicit output recovery
after automatic collection retries were exhausted.

The comparison below uses equal cohorts from rounds 1, 2 and 4: three 52-job
cohorts, or 156 jobs per mode, sleeping 20 seconds each. Mode order rotated
between rounds. Round 3 was excluded from comparative timings because it
included an operator pause and a service restart. Dispatch time runs from the
first execution request to receipt of the last native ID in that cohort.
Scheduler-query and helper-source totals cover the three cohorts per mode.

| Mode | Median time for 52 native IDs | Scheduler status queries | Helper source transferred | Peak worker RSS |
| --- | ---: | ---: | ---: | ---: |
| Standard | 5.621 s | 1,374 | 63.094 MiB | 70.29 MiB |
| Batched | 5.063 s | 306 | 50.955 MiB | 70.34 MiB |
| Persistent | 5.141 s | 1,552 | 0.463 MiB | 69.59 MiB |
| Combined | 5.255 s | 325 | 0.424 MiB | 70.19 MiB |

Batching reduced measured scheduler-status traffic by **77.7%**. Persistent
helpers reduced helper-source transfers by more than 99%, without establishing
a consistent throughput advantage in these few trials. Source-byte figures
exclude SSH framing, input/output data and controller HTTP traffic. Adaptive
polling changes the number of observations, so query counts measure actual
traffic rather than identical observation workloads. Persistent helper memory
is additional to worker RSS; the recorded aggregate helper high-water figures
are not simultaneous RSS measurements.

Output completion was dominated by publication contention rather than native
execution failures. Across the 624 comparison jobs, 130 first collection
reports were incomplete. The project-wide publication lock returned
`UPLOAD_BUSY`; most collections recovered automatically. The final persistent
cohort required three explicit recovery requests, so its output-completion
timing includes an operator pause and is excluded. For the two uninterrupted
rounds shared by every mode, median complete-output times were 56.054 s
(standard), 50.091 s (batched), 78.698 s (persistent) and 72.330 s (combined).
These small, shared-cluster samples do not isolate a causal transport effect.

Each mode also passed a separate eight-job recovery trial: one successful
submission response was discarded after the remote journal recorded its
receipt, one job was deliberately cancelled, and the owned SSH master was
stopped. Automatic reconnection and journal reconciliation preserved all native
IDs; seven successful jobs' outputs and the cancelled job's accounting were
verified per mode. Missing outputs from deliberately cancelled jobs were
expected. An initial standard-mode trial used an incorrect fault injection
which also released ownership descriptors; its eight jobs were reconciled and
verified, and the trial was repeated with fresh jobs. The batched trial's
original harness waited for cancelled-job outputs; its recovery checks passed,
but its timings were excluded. These are test-driver corrections, not changes
to application recovery code.

Sixteen additional jobs per mode produced 1 MiB outputs successfully. Payloads
were identical within each size, so deduplication means this was an output-size
and integrity check, **not a sustained unique-data bandwidth test**. Native
queue waits, measured entirely from Slurm timestamps, had a median of two
seconds and a maximum of 20 seconds across the run. Accounting comments were
not retained by this site; the independent audit checked exact work-directory
bindings and native IDs instead. Losing a controller response after a durable
remote receipt does not test every possible interruption before that receipt
is written.

Capacity and responsiveness findings remain open in the [roadmap](roadmap.md):

- The former 1,000-object quota rejected work after 476 admissions, despite only
  506 KiB of stored data. The maintained default is now 10,000 objects; byte and
  incomplete-transfer limits remain in force.
- The 32 MiB logical-metadata quota rejected preparation after 788 admissions.
  A temporary 64 MiB ceiling in the disposable benchmark launcher allowed the
  test to finish. The normal application's metadata default remains 32 MiB.
- The portal stopped responding once during preparation and recovered after a
  restart. Its cause remains unconfirmed; private stack diagnostics were enabled
  for the remaining trials. This interruption is excluded from transport timings.
- Retained records, artifact quotas, submission allowances and outstanding-work
  capacity are separate limits. Raising a submission allowance alone cannot
  provide bounded burst admission or artifact retention.

For this site, batching is the useful simplification candidate. The persistent
transport and stream handler alone add 264 production lines plus integration
branches and tests; their source-byte savings did not resolve the observed
collection failures or establish a clear dispatch advantage. Choose the
supported path explicitly before removing the alternative. The experiments
remain off by default pending that decision and the reliability fixes.

### Separate MCP acceptance

After the HTTP transport comparison and its native audit finished, a fresh
project ran **200 additional jobs through the installed MCP stdio server using
the official SDK client**. This kept MCP traffic out of the transport timings.
The jobs requested the same resources, slept five seconds and ran in four
groups of 50. Eight MCP calls could be in flight at once. The workload included
200 new preparations, 200 idempotent preparation replays, two 100-job listing
pages, 200 compact job reads, detailed status polling, 200 execution requests
and 200 downloaded and hashed result artifacts.

The preparation burst took **14.405 seconds**. Replays returned the same 200 job
IDs and admitted no extra jobs. All 200 jobs succeeded, with exactly 200 unique
native IDs and matching Slurm/controller CPU accounting. The complete MCP run
took **206.446 seconds** and used 0.587 CPU core-hours. Its 2,441 tool calls
returned no tool errors, and no job needed an explicit output-recovery request.
Internal connector traffic nevertheless encountered 48 `UPLOAD_BUSY` responses
and one `STORAGE_QUOTA` response; all recovered automatically. The generic quota
error does not identify its exact branch. Given the small objects and available
byte/object capacity, the eight-incomplete-transfer limit is the likely cause.
Final inspection found zero pending transfer reservations in both projects.

| MCP operation | Calls | Median call time | 95th percentile |
| --- | ---: | ---: | ---: |
| New preparation | 200 | 0.571 s | 0.988 s |
| Idempotent preparation replay | 200 | 0.131 s | 0.176 s |
| Compact job read | 200 | 0.142 s | 0.221 s |
| Detailed status read | 1,236 | 0.179 s | 0.374 s |
| Execution request | 200 | 0.269 s | 0.450 s |
| Result artifact retrieval | 200 | 0.011 s | 0.023 s |

Call times exclude waiting for the client's eight-slot semaphore; the complete
burst time includes that wait. Mean serialized SDK tool-result sizes were
2,243 bytes for compact reads and 10,479 bytes for detailed reads. These sizes
include the SDK result representation, not the full JSON-RPC or transport
overhead. This verifies a 200-job burst through one local MCP session; it does
not establish unbounded concurrency or remote HTTP MCP support.

Across both tests, Slurm verified **1,144 native jobs**, including four deliberate
cancellations, in approximately **82 minutes** from the original start. Total
CPU charge was 11.052 core-hours, with no GPU use or interactive-partition jobs.
The normal 32 MiB project metadata ceiling and existing retained-record limits
remain separate from the temporary benchmark ceiling and submission grants.

### Architectural interpretation

The tests establish successful recovery in the exercised cases, not a lower
failure rate for either transport. The recommendation for batching over one-shot
helpers is an architectural judgment for this product: fewer application-owned
lifecycle states, while reducing load on the shared scheduler.

Both approaches already share an authenticated OpenSSH master. The persistent
experiment keeps Python helpers alive within that connection; it does not
introduce SSH connection reuse. OpenSSH provides connection multiplexing through
[ControlMaster](https://man.openbsd.org/ssh_config#ControlMaster). Our one-shot
transport opens commands over the protected master and refuses a fresh login
when that master is absent.

One-shot commands pay for process startup and retransmit helper source, but
their process memory and protocol state end with each command. Persistent
helpers avoid that repeated work while adding pool admission, channel ownership,
framing, configuration pinning, timeout disposal and shutdown/reconnection
coordination. The implementation already defends these boundaries; additional
states mean additional invariants to maintain, not proof of an existing defect.
Durable job identity and submission receipts remain in journals in both modes,
so losing a helper does not by itself imply losing or duplicating a native job.

The current persistent transport also retains the one-shot implementation for
discovery and environment inspection. Choosing persistence therefore preserves
both mechanisms unless its coverage and deployment contract are expanded.
Choosing one-shot helpers allows the extra stream protocol and pool to be
removed while retaining coverage of the existing operations.

For hundreds of jobs, bounded batching is useful even without faster dispatch:
Slurm's [squeue performance guidance](https://slurm.schedmd.com/squeue.html#SECTION_PERFORMANCE)
asks clients to minimize scheduler RPCs because excessive calls can degrade the
shared controller. A batch failure can temporarily delay status for several
jobs; it must preserve their independent identities, accounting and reservations,
and retry observation without repeating compute. A single status interface
accepting one through a bounded number of jobs avoids maintaining separate
single-job and multi-job status implementations. Collection should retain
independent per-job recovery and bounded transfer capacity rather than acquire
batch-wide completion semantics.

Persistent helpers become a stronger choice if measurements on target sites
show that process/channel startup, helper-source bandwidth or login-node CPU
limits constrain the required workload. High network latency alone is not proof:
both choices still exchange requests and responses over SSH. A representative
high-latency or sustained-load test would need to demonstrate enough benefit to
justify the extra lifecycle machinery. The present results support one shared
SSH transport with bounded status batching as the general default for this
codebase; they do not establish a universal rule against persistent RPC services.

## Promotion criteria and remaining work

Both options remain off by default. The tests cover
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
