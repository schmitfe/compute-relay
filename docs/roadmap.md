# Open work

One backlog: P1 blocks release readiness, P2 is accepted follow-up work, and P3
needs concrete user/site demand. Shipped behavior belongs in the guides.

## P1 — release readiness

| Work | Completion gate |
| --- | --- |
| Site recovery | Record interruption/recovery on advertised authentication, accounting and storage classes, including VPN loss and delayed accounting where controlled tests are permitted. Follow [site acceptance](testing.md#real-cluster-acceptance). |
| Environment publication recovery | A post-commit hook failure can leave a settled build `awaiting_finalization`. Retry its accepted durable report through normal operation using existing `sync_from_report`; preserve metadata/generation validation and prove no remote rebuild, duplicate environment or charge. |
| Broader beta acceptance | Complete the [capability matrix](testing.md#first-beta-release-gates) for GPU jobs, agent-created venvs, DAGs, advisory storage and investigations. Existing smoke scripts alone are not pass evidence. |
| macOS for 0.1 | Fix filesystem-name and process-test portability, add Mac CI and verify the desktop/SSH lifecycle. Required checks are in the [beta matrix](testing.md#first-beta-release-gates). |
| Portal responsiveness | Capture and diagnose a recurrence of the unexplained four-minute stall, then test its cause and concurrent recovery. [Stack capture](runtime.md#diagnosing-an-unresponsive-portal) is available; successful synthetic bursts have not established a fix. |

## P2 — accepted follow-up work

| Work | Completion gate |
| --- | --- |
| Usability acceptance | Novice/experienced walkthroughs: setup, workspace handoff, second cluster, restricted keys, bursts, cancellation, failed collection and verified results. Include keyboard/no-JavaScript use and live updates that preserve edits/views. |
| Storage integration | Route new staging/build writes to selected locations and advertise protected roots before claiming reservations. Keep capacity advisory and native quotas unknown until measured; never attribute bytes to another target. See [storage](extensions-storage.md). |
| Module-based environment selection | Select a Conda module, discover environments and grant several cluster-bound revisions. Verify interpreter/prefix on compute nodes; permission to use an environment remains separate from permission to build. |
| Module-aware builders | Derive venv/pip/uv, Conda or Poetry prerequisites from the approved module; replace generic bootstrap errors. Preserve pinned inputs, approval, limits, protected prefixes and compute-node verification. Only venv builds exist today. |
| Intermediate results | Publish declared immutable chunks during execution with atomic publication, stable hashes, bounded transfers and shared quotas. Protect snapshots from cleanup. Until then, use chunk jobs with dependent analysis; live logs already work. |
| Throughput/wait monitoring | A bounded portal graph by project/cluster for queue, execution and collection times, with sample counts and unknowns. Separate scheduler delay from relay delay before adding a monitoring stack. |
| Optional cost/placement module | Compare suitable targets using owner-configured hardware rates, predicted runtime, allocation rounding, queue and transfer cost. Show uncertainty; estimates cannot grant compute authority. |
| Retention/environment lifecycle | Opt-in age/pinning rules, metadata compaction and retirement of unused uploads/environments. Extend [manual cleanup](deployment.md#manual-file-cleanup) with remote byte previews and storage accounting; protect active/uncertain work, pending collection, referenced results and writer-owned transfers. |
| Admission tuning | Test sustained mixed-project bursts beyond the MCP fixture and site fairness before raising defaults. Keep spending, outstanding work, rate and retained metadata separate; [current bounds](runtime.md#burst-outstanding-work-and-retained-data-limits) are implemented. |
| Collection/storage load | Verify finite recovery and cleanup under independent writers and multi-GiB outputs. Preserve exact accounting, generation fencing and integrity checks; distinguish recoverable contention from authority/integrity failures. |

## Planned first beta

Target `0.1.0b1`, with `0.1.0` reserved for the final release: one human owner,
Linux/macOS, CPython 3.12–3.14, single-node Slurm CPU/GPU jobs, existing
environments, owner-authorized agent-created Python venvs, branching/joining
DAGs, advisory storage and investigations. This is a target, not completed
acceptance. Shared multi-user hosting and Conda/Poetry builders are excluded.
Complete P1 and the [release checklist](testing.md#first-beta-release-gates).

Incompatible beta updates retain [fresh setup](deployment.md#alpha-updates-and-fresh-setup):
reconcile outstanding work and preserve results/evidence first. Same-version
restart/crash recovery remains required. Separate service and agent UIDs need
an isolation walkthrough before recommendation; see [owner-session boundaries](security.md#local-human-authority).

## P3 — only on demand

- Investigation placement/sub-budgets and richer progress, using the existing
  authority and ledger. [Current behavior](investigations.md) remains bounded/advisory.
- Other environment languages, Slurm arrays, alternative schedulers, remote
  HTTP MCP/OAuth, generated OpenAPI/admin APIs, packed environments or direct/
  object-store delivery. Each needs its own authorization/accounting acceptance.
  Restore a backend selector only with a second supported scheduler.

## Optional maintenance

- Validate cookie/account isolation before supporting untrusted local services;
  private-desktop launch-token behavior is currently accepted.
- Add browser hardening, upload headroom checks or wait fairness only for a
  demonstrated need; preserve current integrity and admission bounds.
- Add typed boundaries, explicit hooks, small extractions or lint/type checks
  when touching those paths; avoid a framework rewrite for an audit checklist.
- Add upgrade migrations only when in-place upgrades become supported.
- Retire inert diagnostic credentials on cluster removal and handle cancelled-
  draft retention through the lifecycle work above.
