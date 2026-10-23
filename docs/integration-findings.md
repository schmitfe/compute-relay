# Follow-up to the structured Ising workflow acceptance

An operator-supplied scientific acceptance report exercised a parent agent,
delegate, local simulation, ordinary cluster execution and retained artifact
download. It reported byte-identical seeded scientific outputs between its two
execution environments, repeated staging reuse and idempotent preparation.
Those scientific assertions belong to that test run; broker regression tests
do not establish reproducibility for arbitrary simulations or Python versions.

The follow-up addresses three concrete infrastructure findings without
resubmitting the scientific workload.

## Logical CPU allocation and reservation prediction

The saved job requested one logical CPU and one minute. Its package reserved
60 CPU-seconds, but Slurm reported two allocated logical CPUs for two seconds,
so accounting correctly charged four CPU-seconds. Actual runtime remained below
the reservation. The discrepancy was in allocation prediction.

A bounded read-only discovery refresh found `select/cons_tres` with
`CR_CORE_MEMORY` and two threads per core on both eligible nodes. Whole-core
allocation therefore explains a request of one logical CPU receiving two.
See [Slurm consumable resources](https://slurm.schedmd.com/cons_tres.html) and
the [sinfo topology fields](https://slurm.schedmd.com/sinfo.html).

Discovery now records sockets, cores per socket and threads per core. A pure
planning helper suggests a provisional CPU allocation unit only for supported,
complete and internally consistent scheduler/topology evidence. Partition
overrides and selected partitions are considered. Heterogeneous units combine
through their least common multiple, rather than assuming the largest unit
always bounds every rounding case. Unsupported or incomplete evidence remains
unknown. Suggestions never silently rewrite an active worker profile.

The owner can save `cpu_allocation_quantum` through **Cluster configuration**.
The model is:

```text
effective CPUs = ceil(max(requested CPUs, configured floor) / unit) * unit
reserved CPU-seconds = effective CPUs * requested walltime
```

With a unit of two, CPU requests 1, 2, 3 and 4 reserve for 2, 2, 4 and 4 CPUs.
This is separate from a minimum floor, which cannot model odd counts correctly.
Admission, project/site ceilings, pending approval reservations and test-job
planning use the same model. Walltime gains no margins. Final accounting still
charges observed allocation and elapsed time, including overages.

Context includes the configured unit, the largest CPU request fitting the
current envelope, and whether prediction has an explicit unit. An unspecified
unit remains an unverified request/floor estimate. Attempt summaries compare
prepared and observed allocation and flag an exceeded prediction separately
from whether actual usage exceeded the time-based reservation.

Applying a new unit requires an updated connector advertising
`personal_cpu_allocation_v1`, the ordinary owner configuration operation and
worker acknowledgement. Older workers receive a clear restart message before
any configuration task is created. Existing packages and completed accounting
are preserved; prepare a new package after the configuration revision changes.
Site submission plugins, later configuration changes and account/QoS rules can
still affect allocation. The configured model is not a universal scheduler
guarantee.

## Compact response and running-adapter identity

The previous report recorded a verbose `get_job` result. The then-current
implementation already had a compact default. Without its actual call arguments
and loaded implementation, the saved response length cannot determine whether
it used an old process or explicit full detail.

Regression tests now prove through real stdio that omitting `detail` and passing
`detail=False` produce identical compact responses. `detail=True` remains the
explicit full view. Each response carries its mode. Context and initialization
instructions identify the local adapter implementation, package/schema version
and import-time source fingerprint. A running process retains its identity
after files change; reconnecting starts the new implementation. This stamp is
not an attestation of the broker service or optional extensions.

`get_project_allowance()` now returns budget, reservation, debt and submission
counts without repeating the compute catalog. Use `get_project_context` for
discovery and `get_project_allowance(detail=True)` for the previous full view.
REST contracts remain unchanged. Job compaction still occurs after the existing
HTTP response is fetched, so the server summary endpoint remains roadmap work.

## Helper discovery without a PATH assumption

Initialization now generates a private, ignored `.compute-broker/USAGE.md` with
concrete commands using the installed absolute Python interpreter and
`-m compute_broker.cli`. Shell quoting handles spaces and apostrophes in both
the interpreter and workspace path. The shared skill, project guidance and
initializer output all point to this guide. It contains no API-key bytes and
does not require a globally installed console script.

Explicit `init-mcp --reuse-key --with-workflow` upgrades exact known generated
skills and managed blocks while preserving surrounding instructions, keys and
client settings. Unrecognized edits are refused before mutation. Codex and
Claude Code continue using the same skill and broker implementation.

The follow-up does not automatically modify the external science workspace or
enable new cluster authority. Operator acceptance should reconnect the MCP
adapter, refresh initialization, restart an outdated connector, save a verified
unit, and inspect a newly prepared reservation before deciding on another run.
