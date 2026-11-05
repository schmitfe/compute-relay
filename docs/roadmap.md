# Open work

One backlog: P1 blocks the named release gate, P2 is accepted follow-up work, and P3
needs concrete user/site demand. Shipped behavior belongs in the guides.

## P1 — release readiness

| Work | Completion gate |
| --- | --- |
| Site recovery | Before advertising additional site classes, record interruption/recovery on their authentication, accounting and storage, including VPN loss and delayed accounting where controlled tests are permitted. Follow [site acceptance](testing.md#real-cluster-acceptance). |
| Broader beta acceptance | Keep exact-candidate CI green and record accepted site classes against the [capability matrix](testing.md#first-beta-release-gates). Native CPU/GPU and module evidence is in [runtime](runtime.md#acceptance-evidence-and-remaining-reliability-work); fixture success does not certify every institutional site. |
| macOS for 0.1 final | First-beta support is provisional after green Apple Silicon release CI. Before `0.1.0` final, complete a human desktop walkthrough including browser launch, terminal MFA, reconnect and shutdown. Filesystem aliases, process checks and system-OpenSSH lifecycle are automated. Intel Macs are outside the tested support matrix. |
| Release publishing | The PyPI trusted publisher and protected GitHub `pypi` environment are configured. Verify prerelease metadata and complete the [release checklist](testing.md#first-beta-release-gates). Publication is release-only after platform and disposable Slurm checks pass. |

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
DAGs, advisory storage and investigations. Linux capability acceptance is recorded
in [runtime](runtime.md#acceptance-evidence-and-remaining-reliability-work); macOS is
provisional until its human desktop acceptance. Shared multi-user hosting and
Conda/Poetry builders are excluded.
Complete the applicable [release checklist](testing.md#first-beta-release-gates).

Incompatible beta updates retain [fresh setup](deployment.md#beta-updates-and-fresh-setup):
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

- Investigate the unreproduced portal stall only if it recurs; keep [stack capture](runtime.md#diagnosing-an-unresponsive-portal) available. It is deferred, not a demonstrated fix.
- Validate cookie/account isolation before supporting untrusted local services;
  private-desktop launch-token behavior is currently accepted.
- Add browser hardening, upload headroom checks or wait fairness only for a
  demonstrated need; preserve current integrity and admission bounds.
- Add typed boundaries, explicit hooks, small extractions or lint/type checks
  when touching those paths; avoid a framework rewrite for an audit checklist.
- Add upgrade migrations only when in-place upgrades become supported.
- Retire inert diagnostic credentials on cluster removal and handle cancelled-
  draft retention through the lifecycle work above.
