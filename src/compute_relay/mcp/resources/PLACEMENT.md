# Placement and environments

Read get_project_context once for permitted targets, effective limits,
environments, review requirements and shared allowance. Refresh focused data
when it changes or a resource/budget rejection requires a new decision.

Choose local work for derivations, analysis and small checks that fit the device
actually running the code. Use a hybrid workflow for development and analysis
locally with larger cluster stages. A sandbox, client device and broker host can
have different CPU, RAM and interpreter limits. Estimate peak RAM, GPU VRAM,
temporary storage, output size and concurrency. Count numerical-library threads
together with process workers. A bounded pilot should measure the uncertain
scaling assumption before the planned production run.

`resources.memory_mib` requests total host RAM in MiB. It does not request GPU
VRAM. Select a GPU type whose per-device memory fits the actual workload; a large
host-memory allowance does not make a matrix fit on the GPU. Independent cluster
CPU/GPU/RAM maxima may describe different nodes or partitions. Match the target,
partition, environment and effective allocation limits. Requested walltime
determines the reservation; final allocated runtime determines the charge.

For placement across targets, request get_compute_availability(refresh=True)
then read refresh=False. Check age, status, truncation and visibility. Compare
queue_pressure.pending_jobs_per_schedulable_node and the matching GPU type's
pending_gpu_jobs_per_schedulable_device numerically; these ratios are decimal
strings or null. Ten pending jobs on five schedulable nodes is 2 per node;
100 on 200 is 0.5. Use raw counts, job size and capacity as well. Partitions can
overlap; idle resources may be fragmented, reserved or inaccessible. The snapshots
do not predict queue wait, guarantee placement or estimate monetary cost.

With the environments extension, inspect observations and get_environment_inventory
before relying on a registered label. inspect_environment requests a small
scheduled runtime/package probe under normal approval and budget rules. Reuse a
pending probe; resume failed preparation through inspect_environment rather than
revise_job. Sole configured partition/account/QOS choices may be filled; omitted
resources retain a saved request. Probe revisions remain immutable once execution
has been requested. A refreshed inventory records observation time and fingerprint,
not a permanent promise about the environment.

For missing packages, read get_environment_build_policy and upload a complete,
hash-pinned wheel closure with the workspace helper. request_environment_packages
builds from an approved bootstrap or updates a managed environment into a new
immutable revision. Existing pinned requirements remain unless explicitly
replaced. Check permitted_for_jobs: verified builds become usable automatically
only if the owner enabled auto_permit_builds. Otherwise use the human promotion
path. Python inventory and builds are the implemented scope.

Use actual observed accounting when allocation exceeds the preparation estimate
and flag the cluster configuration. One observed CPU allocation does not establish
a general rounding rule; agents do not change cluster policy to make a job fit.
