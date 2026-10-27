# Large files and live job output

Compute Relay transfers files up to 8 GiB each in both directions, with a 64 GiB
aggregate job limit. The workspace CLI, broker storage and cluster helper pass
1 MiB chunks instead of putting complete files into a job lease or terminal
report. Transfers use the existing authenticated HTTP and protected SSH paths;
no additional remote daemon or rsync installation is needed. This is a resumable
whole-file protocol, not rsync's block-delta algorithm. JSON chunks use base64
on the wire, so encoded traffic is approximately one third larger than file data.

## Upload inputs and download results

Initialize the science workspace with `compute-relay init-mcp`, then select
explicit files as usual:

```sh
compute-relay files stage main.py data/initial-state.bin
compute-relay files prepare --bundle BUNDLE_SHA256 --request job.json
```

The bundle receipt contains paths, sizes and hashes. The reviewed entrypoint is
limited to 1 MiB; other declared inputs may use the full file limit. Files above
512 KiB use resumable upload sessions. Repeating `files stage` reuses completed
authorized objects and resumes matching incomplete uploads. Repeated chunks
must contain identical bytes. A partial upload is not input authority: only
receipt and verification of the complete independently supplied file establish
an upload grant.

The worker stages large inputs before confirming dispatch and recording submit
intent. A failed transfer therefore cannot become an uncertain scheduler
submission. The helper verifies the staged files against the immutable manifest
before calling `sbatch`.

```sh
compute-relay files fetch --attempt-id ATTEMPT_UUID --output results/run-1
compute-relay files fetch --attempt-id ATTEMPT_UUID --output results/run-1 --resume
```

The first command creates a new private destination. `--resume` requires its
matching attempt/inventory plan, verifies completed files, and continues partial
files from their received offsets. Complete files are published only after
their byte count and SHA-256 match. The final `.compute-relay-receipt.json`
records transfer integrity separately from scientific validation. A failed
checksum never produces a success receipt. If a partial file itself is damaged,
choose a new destination. Ordinary fetches never overwrite existing paths.

`files finish --job-id JOB_UUID --output results/RUN` consolidates provenance,
completion state and verified downloads into `run-manifest.json`. With the
explicitly enabled investigations module, `--investigation-id` exports the
whole group. `--resume`, `--no-download` and explicit `--acknowledge` preserve
the distinction between retained artifacts, local integrity and scientific
acceptance. See [completion and its consistency limits](investigations.md).

## Output recovery without another compute run

Execution state, accounting and `collection_state` are separate. A successful
native job can have `collection_state=retry_pending` or `failed`. Collection
failures receive at most three automatic attempts, with 15 and 30 second retry
delays. Completed output objects and incomplete transfers are reused on retry;
the worker does not resubmit the job or charge compute again. Missing declared
files are reported as collection failures. After automatic retries stop:

```sh
compute-relay files retry --attempt-id ATTEMPT_UUID
```

Agents can also call `retry_job_outputs(attempt_id)`. Both require `jobs:write`.
Recovery is limited to the original declared outputs and exact native attempt.
It works with compatible journals from before this upgrade, including jobs
whose original collector hit the 10 MiB limit, provided the remote files remain.

## Follow stdout and stderr

```sh
compute-relay logs --attempt-id ATTEMPT_UUID --follow
compute-relay logs --attempt-id ATTEMPT_UUID --stream stdout --follow
```

The worker reads running-job logs on status observations, normally every 2–5
seconds while running. The CLI checks the broker every two seconds; connector
load, SSH latency or an unavailable connector can delay updates. The portal's
existing live attempt panel displays the same collected text. MCP clients use
`get_job_logs` and retain `next_cursor` for their next call. These are incremental
snapshots, not an open SSH pipe or a push notification to an inactive agent.

Each stream retains its most recent 64 KiB. Byte cursors, `available_from`,
`truncated`, `reset`, `file_id`, `updated_at` and `observed_at` identify gaps,
replacement and freshness. `eof` means the current snapshot is drained;
`finished` separately identifies a terminal execution. `content_base64`
preserves the exact raw bytes; `text` is a UTF-8 display with replacement for
invalid bytes. Consumers should compare `file_id` between calls to recognize
replacement. The CLI decodes UTF-8 incrementally across slices. Logs remain
untrusted program output.

New batch scripts set `PYTHONUNBUFFERED=1`, making Python prints visible without
waiting for process exit. Other programs must flush their own output. Existing
batch scripts keep their approved contents and therefore retain their original
buffering behavior. Full log archival is separate: this bounded progress window
does not preserve an entire multi-gigabyte stdout/stderr history.

## Storage and upgrade

Defaults are 64 GiB per project, 256 GiB per installation, 1000 objects per
project and eight incomplete transfers per project. Configure byte quotas with
`BROKER_PROJECT_STORAGE_LIMIT` and `BROKER_GLOBAL_STORAGE_LIMIT`;
`BROKER_MAX_TRANSFER_BYTES` can lower the per-file limit. The full declared size
is reserved before receiving chunks. Disk headroom and project/global quotas
still apply. Incomplete sessions expire 24 hours after their last acknowledged
chunk; bounded maintenance releases their reservations. Hashing and file I/O
use bounded buffers, and final object hashing runs outside SQLite write locks.

Upgrade the broker and connectors together and apply the bundled database
migration through the normal launcher. Restart MCP clients to discover the new
recovery tool. Newly submitted packages must be prepared against the installed
helper revision. Already submitted attempts retain their original manifest and
native identity and remain manageable through the compatible journal protocol.
No credentials or remote listeners are added by this change.

## Verification

Default tests exercise uploads, resumable downloads, stale leases, scopes,
checksum/conflict rejection, quota reservations, live byte cursors, file changes,
symlink rejection, and synchronous/asynchronous collection beyond 10 MiB.
An additional local acceptance sends a file larger than 2 GiB through all four
legs and checks Python allocation bounds:

```sh
COMPUTE_RELAY_LARGE_TRANSFER_TEST=1 python -m pytest \
  tests/unit/test_large_transfers.py::test_multigib_roundtrip_is_bounded_across_workspace_broker_and_worker -qs
```

It uses a disposable database, real local files, HTTP handlers and the fixed
cluster helper with a scheduler fixture that never executes uploaded code.
It does not establish real-network throughput or real Slurm behavior.

The [recorded local acceptance](benchmarks/2026-10-27-resumable-transfers.json)
transferred 2,147,483,665 bytes through each of the four legs, verified the final
checksums, and measured 122,474,955 bytes (about 117 MiB) of peak traced Python
allocations. The elapsed time includes allocation tracing and fixture overhead
and is not a network throughput measurement. Persistent-helper tests separately
exercise actual framed helper processes for large inputs, outputs and live logs.
