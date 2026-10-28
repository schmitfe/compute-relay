# Open work

This is the single backlog of accepted work and deliberately deferred
capabilities. P1/P2 items are accepted; P3 items need concrete user or site
demand before they become commitments. Importance reflects user risk and
release readiness, not convenience.

| Priority | Work | Why it matters / completion gate |
| --- | --- | --- |
| **P1 — release gate** | Publish a verified private security-reporting channel, supported-version policy and response ownership. | `SECURITY.md` currently describes fallback reporting. Do this before claiming release support. |
| **P1 — deployment evidence** | Run and record acceptance on each supported target class, including a permitted real Slurm site. | Synthetic tests do not prove site-specific authentication, accounting, software availability or filesystem behavior. Follow [testing guidance](testing.md#real-cluster-acceptance). |
| **P2 — storage integration** | Advertise protected storage roots from the worker and connect estimates to the actual write target before claiming storage reservations. Add site-specific quota reporting only where required. | Current free-space reports are advisory and native user quotas remain unknown. Never attribute environment-build bytes to a different selected target. See [storage limits](extensions-storage.md). |
| **P2 — environment lifecycle** | Define managed-environment retention, safe cleanup and storage-target accounting before adding automated deletion or migration. | Built environments currently remain in protected attempt staging; jobs may still depend on them. Preserve active references and verify ownership before cleanup. See [environment limits](environment-builds.md#storage-retention-and-current-limits). |
| **P2 — transport rollout** | Benchmark the opt-in batched and persistent-helper paths on permitted sites with varied latency, active-job counts and output sizes before changing defaults. | The disposable two-CPU fixture establishes correctness and traffic reductions, not production performance. See [experiment evidence](batched-persistent-experiment.md#promotion-criteria-and-remaining-work). |
| **P3 — investigation guidance** | Add explainable local/cluster placement, enforceable investigation sub-budgets, richer rate-limited progress and further summary pagination only if users need them. | Placement and investigation allowances must share current authority and reservation rules. Existing waits, closeout and bounded summaries remain the supported behavior. See [workflow design](portable-compute-workflow.md). |
| **P3 — only on demand** | Add other environment managers/languages, native Slurm arrays, alternative schedulers, remote HTTP MCP/OAuth clients, generated OpenAPI/admin APIs, large-data transfer, packed environments, or direct/object-store artifact delivery. | These expand supported contracts and security surface. Implement only for a concrete user/site need, with separate authorization, accounting and acceptance criteria. Current limits are in [runtime](runtime.md), [backends](backends.md) and [module guides](extensions.md). |
