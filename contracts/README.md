# Contracts and examples

Canonical JSON Schema 2020-12 files ship in `src/compute_relay/schemas`.
Runtime and `tools/check_contracts.py` use the same offline validator; `.invalid`
identifiers resolve locally.

| Schema | Meaning |
| --- | --- |
| [common](../src/compute_relay/schemas/common.schema.json) | Logical paths, file descriptors, single-node resources and charge vectors |
| [job request](../src/compute_relay/schemas/job-request.schema.json) | Client inputs without SSH/raw scheduler/approval authority |
| [prepared job](../src/compute_relay/schemas/prepared-job.schema.json) | Complete rendered manifest; digest is external |
| [project policy](../src/compute_relay/schemas/project-policy.schema.json) | Human-managed limits and admission policy |

Site/project/key settings only narrow protocol bounds. Sources are explicit regular
files; outputs are exact relative paths, never globs. `_broker/` is reserved.
No data URLs, implicit archive import, custom installer hooks, arrays/multiple
nodes, user-controlled environment variables or raw scheduler arguments are accepted.
Explicit ZIP helpers operate locally or in approved jobs. `memory_mib` is host RAM,
not GPU VRAM. GPU jobs require an allowed type unless explicit
[generic-GPU permission](../docs/management.md#projects-and-job-policy) applies;
CPU-only requests omit it.

Validation rejects duplicate/colliding paths, missing entrypoints, inconsistent
prepared descriptors/allocation/reservations, duplicate JSON keys, nonfinite
numbers and unpaired surrogates. Wire integers fit `2^53-1`; internal ledger
arithmetic is checked BIGINT. RFC 8785 canonicalization does not replace validation.
The service also checks ownership, input provenance, actual bytes and authority.

Requested walltime is finite whole minutes. Reservation is effective allocation
(after verified site rounding) × requested walltime, without cleanup/overtime
margins. Settlement records all actual allocated elapsed usage, including overages;
debt blocks new admission and missing accounting retains the reservation. These
are planning commitments, not physical scheduler caps. Changed connection binding
invalidates prepared approval.

In `mixed` mode, `per_job_limits` is the automatic envelope: larger requests need
review while hard cluster/installation limits and allowance still apply. Other
modes enforce hard project limits. Compare effective allocation after rounding,
not just requested counts.

Examples use synthetic UUIDs/digests, not executable bytes:
[CPU request](examples/cpu-job.json), [GPU request](examples/gpu-job.json),
[prepared GPU](examples/prepared-gpu-job.json), [policy](examples/project-policy.json).
Contract tests supply real bytes and negative cases. Transfer bounds are maintained
in [runtime](../docs/runtime.md#local-storage-and-metadata-bounds).
