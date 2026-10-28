# Version one contracts and examples

The canonical JSON Schema 2020-12 contracts live in
[`src/compute_relay/schemas`](../src/compute_relay/schemas) and are included in the
installed package. Runtime requests and `tools/check_contracts.py` use the same
offline validator in [`domain/validation.py`](../src/compute_relay/domain/validation.py).
This directory keeps the examples and contract guide. The `.invalid` schema
identifiers remain stable local identifiers; references resolve entirely offline.

| Contract | Meaning |
| --- | --- |
| [common.schema.json](../src/compute_relay/schemas/common.schema.json) | Safe logical paths, immutable file descriptors, single-node resources, integer charge vectors |
| [job-request.schema.json](../src/compute_relay/schemas/job-request.schema.json) | Agent-provided request; contains no SSH credentials, host, raw scheduler directives, URLs, or approval authority |
| [prepared-job.schema.json](../src/compute_relay/schemas/prepared-job.schema.json) | Complete manifest after trusted rendering; its digest is external to avoid a self-reference |
| [project-policy.schema.json](../src/compute_relay/schemas/project-policy.schema.json) | Human-managed project limits and admission policy |

The constants are intentionally conservative protocol bounds. Site, project and key limits can only narrow them. `gpu_type` is mandatory for GPU jobs and absent for CPU-only jobs. Sources are regular files uploaded individually. Output selectors are exact relative file paths, not globs; stdout/stderr are bounded service-defined outputs. Uploaded names cannot use the reserved `_broker/` namespace. Version one rejects data URLs, archive imports, custom environment installers, arrays, multiple nodes, user-controlled environment variables and arbitrary native scheduler arguments.

Schema validation is necessary but insufficient. The shared validator also rejects duplicate paths, requires the entry script, prevents input/output collisions, bounds aggregate file sizes, and checks prepared descriptors, allocation and reservation calculations. The service additionally authorizes references, resolves ownership, checks policy/capabilities, verifies actual file bytes, reserves compute and enforces lifecycle rules. Example descriptors use the runtime limits of 8 GiB per file and 64 GiB per package.

The prepared allocation can exceed requested CPU/GPU counts if a tested site model rounds up; it cannot be lower. Requested scheduler walltime is an exact whole number of minutes in version one. Reservations multiply these effective allocated cores/devices by requested walltime. No termination, overtime or cleanup interval is added. Final charges use actual Slurm-reported allocated resource time, including elapsed time beyond the reservation. An overdrawn project cannot admit new jobs until its debt is resolved. The prepared manifest also binds an immutable connection-configuration digest, so target changes invalidate approval.

The current version-one submission contract requires finite requested walltime and effective resource allocation for both human-reviewed and automatic execution. The reservation is a planning commitment, not a physical upper bound on scheduler usage. Missing final accounting preserves the outstanding reservation until reconciliation.

Wire integers are limited to the exact JSON/browser range through `2^53 - 1`; internal database counters use checked BIGINT arithmetic. Reject duplicate JSON keys, nonfinite numbers and unpaired Unicode surrogates before hashing or schema validation. Prepared manifests use the RFC 8785 library in `backends/preparation.py`; schema validation does not replace canonicalization.

`mixed` mode uses `per_job_limits` as the automatic envelope. A larger request needs human approval and still must fit the hard installation/cluster limit and project allowance; it is re-prepared before review. In `approval_required` and `budgeted` modes these limits are hard project admission limits. Compare effective prepared CPU/GPU allocation, memory and requested scheduler walltime against the corresponding limits after site rounding; request-only checks are insufficient. Policy changes are explicit human actions with a new revision. Automatic grant replenishment is not part of version one.

The example set uses synthetic UUIDs and digests; no example hash is evidence of executable file contents. Runtime bundle tests will supply actual bytes and verify hashing and canonicalization. The currently supported CPU-only/GPU request examples and prepared example should validate; negative cases live in `tests/contracts/test_contracts.py`.
