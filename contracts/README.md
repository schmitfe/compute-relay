# Version one planning contracts

These JSON Schema 2020-12 contracts define the first supported request surface. They are planning artifacts, not generated runtime schemas. Runtime serializers must later be checked against them. The `.invalid` schema identifiers are stable local identifiers, not hosted endpoints; tests resolve references entirely offline.

| Contract | Meaning |
| --- | --- |
| `common.schema.json` | Safe logical paths, immutable file descriptors, single-node resources, integer charge vectors |
| `job-request.schema.json` | Agent-provided request; contains no SSH credentials, host, raw scheduler directives, URLs, or approval authority |
| `prepared-job.schema.json` | Complete manifest after trusted rendering; its digest is external to avoid a self-reference |
| `project-policy.schema.json` | Human-managed project limits and admission policy |

The constants are intentionally conservative protocol bounds. Site, project and key limits can only narrow them. `gpu_type` is mandatory for GPU jobs and absent for CPU-only jobs. Sources are regular files uploaded individually. Output selectors are exact relative file paths, not globs; stdout/stderr are bounded service-defined outputs. Uploaded names cannot use the reserved `_broker/` namespace. Version one rejects data URLs, archive imports, custom environment installers, arrays, multiple nodes, user-controlled environment variables and arbitrary native scheduler arguments.

Schema validation is necessary but insufficient. Service validation must also authorize every reference, resolve object ownership, check policy/capabilities, compare content hashes and actual sizes, reject duplicate paths, require the entry script, prevent input/output collisions, reserve the maximum allocation, and enforce lifecycle rules. `tools/check_contracts.py` checks a subset of these cross-field conditions for planning examples only.

The prepared allocation can exceed requested CPU/GPU counts if a tested site model rounds up; it cannot be lower. Requested scheduler walltime is an exact whole number of minutes in version one. A separate `maximum_charge_duration_seconds` includes a verified conservative termination/cleanup margin, including applicable Slurm `KillWait` and `OverTimeLimit` behavior. Reservation units are maximum allocated cores/devices multiplied by this charge duration. Unknown or unbounded margins disable automatic budgeted admission for that connection. Native requeues and usage beyond that envelope are handled as exceptions, not hidden in rounding. The prepared manifest also binds an immutable connection-configuration digest, so target changes invalidate approval.

The current version-one submission contract requires a finite prepared envelope for both human-reviewed and automatic execution. A partition with unknown/unbounded charge duration remains discovery/status-only until a validated site configuration supplies a bound. Human review does not turn an unknown number into a maximum. A separate unbounded-review contract is outside this first release.

Wire integers are limited to the exact JSON/browser range through `2^53 - 1`; internal database counters use checked BIGINT arithmetic. Reject duplicate JSON keys, nonfinite numbers and unpaired Unicode surrogates before hashing or schema validation. These checks do not implement RFC 8785 canonicalization; a vetted canonicalization library and cross-language vectors are required in milestone M1.

`mixed` mode uses `per_job_limits` as the automatic envelope. A larger request needs human approval and still must fit the hard installation/cluster limit and project allowance; it is re-prepared before review. In `approval_required` and `budgeted` modes these limits are hard project admission limits. Compare effective prepared CPU/GPU allocation, scheduler limit and maximum charge duration against the corresponding limits after site rounding; request-only checks are insufficient. Policy changes are explicit human actions with a new revision. Automatic grant replenishment is not part of version one.

The example set uses synthetic UUIDs and digests; no example hash is evidence of executable file contents. Runtime bundle tests will supply actual bytes and verify hashing and canonicalization. The currently supported CPU-only/GPU request examples and prepared example should validate; negative cases live in `tests/contracts/test_contracts.py`.
