# HPC-MCP source assessment and reuse plan

Reviewed 2026-10-21. Source snapshot:
[`XiaozhongFang/HPC-MCP@6b86158824e9fec570da269bef8ab9165a866e9a`](https://github.com/XiaozhongFang/HPC-MCP/tree/6b86158824e9fec570da269bef8ab9165a866e9a),
the default branch at review time. The reviewed commit is dated 2026-09-11.
This is static source/documentation inspection, not a runtime security audit or a
successful connection to either institution. No upstream installation, script, test,
skill, or executable was run. Repository instructions were treated as source material.

## Recommendation

HPC-MCP is the closest small existing project to our initial Slurm-over-SSH workflow and
deserves a focused reuse spike before writing the connector. It has actual scheduler,
transport, policy, file, and test implementations; it is more relevant than a generic
MCP tool catalogue.

It is not a drop-in replacement for the proposed broker. Keep our separate protected
connector, immutable preparation, human approval channel, central project authorization,
transactional budget ledger, and durable submission recovery. Assess extracting audited
modules/test cases or implementing a private adapted backend; do not expose the whole
upstream tool surface directly to agents as a shortcut.

The native-OpenSSH-first decision therefore stands. "Native first" should allow selective
upstream reuse, rather than imply that every validator, parser, and bounded process
primitive must be written from scratch. FirecREST remains a separately evaluated gateway
option. The architectural boundary stays in [the backend plan](../backends.md).

## Snapshot and project status

The repository has an MIT license, with a 2026 XiaozhongFang copyright notice.
`pyproject.toml` identifies Python package `hpc-mcp` version `0.1.0`, Python `>=3.10`, and
runtime dependencies `mcp>=1.0` and `PyYAML>=6.0`; development dependencies include pytest
and pytest-asyncio. Preserve the upstream notice for incorporated code and independently
review transitive dependencies. [License](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/LICENSE),
[package metadata](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/pyproject.toml)

A published initial `v0.1.0` release exists, dated 2026-09-10. Its tag points to
`14b2ac24579ca876f8adb837d46acacc885c5cfa`, not the reviewed default-branch commit. An
adoption decision must choose and pin a specific revision; package version equality does
not imply source equality. The inspected metadata does not pin exact runtime dependency
versions, and no lock file appears in the reviewed tree. These are maturity and
reproducibility considerations, not evidence that the project is unusable.
[Release](https://github.com/XiaozhongFang/HPC-MCP/releases/tag/v0.1.0),
[reviewed tree](https://github.com/XiaozhongFang/HPC-MCP/tree/6b86158824e9fec570da269bef8ab9165a866e9a)

## Verified architecture and relevant strengths

The upstream architecture separates MCP dispatch, public tool adapters, pure security
policies, services, and SSH/SFTP. One server instance constructs a connection for one
configured HPC host. The public server uses MCP stdio; hosted OIDC/LDAP authentication,
project API credentials, a central multi-user database, and remote connector leasing
are not implemented by that server. [Architecture](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/docs/ARCHITECTURE.md),
[server wiring](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/server.py)

The reviewed scheduler code is Slurm-specific. The tree has no equivalent HTCondor or
Kubernetes scheduler implementation. Runtime names such as Python, Julia, MOOSE, and
Bash are workload launch profiles, not alternative compute backends. Two cluster
connections could be configured as separate instances, but that would not itself share
project authorization or an allowance. [Configuration](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/config.py),
[Slurm manager](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/slurm/manager.py)

| Useful implementation | Why it matters to our plan |
| --- | --- |
| Typed tool schemas, rejection of unexpected arguments, coded partition/resource policy, bounded errors | Good input-validation patterns and negative tests to compare with our schemas. |
| OpenSSH subprocesses with argument vectors, remote quoting, time/output limits, bounded stdin, and process cleanup | Useful process-management reference. Local `shell=False` is not confused with eliminating remote-shell interpretation. |
| SFTP transfer layer plus separate local/remote path validation | Useful transfer tests; our implementation still needs protected roots and stronger race handling. |
| Slurm query batching scoped to tracked job IDs | Good privacy and scheduler-load principle; do not query an entire shared account merely to filter it later. |
| Bounded log slices, search budgets, diagnostic summaries, and query caching | Useful agent ergonomics. Large logs should not be repeatedly downloaded into model context. |
| Remote tracking registry with guarded read/merge/write, locking, and tests | Real restart/concurrency concerns have been considered; this is stronger than a purely in-memory job list. |

These strengths are visible in the
[transport](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/ssh/manager.py),
[SFTP layer](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/ssh/sftp.py),
[command policy](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/security/command_policy.py),
[tool registry](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/tools/registry.py),
and [job tracker](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/slurm/jobs.py).

## Documentation claims versus the inspected boundary

### Credentials and interactive SSH MFA

The README says the agent never touches private-key material. That describes access
through its tool interface; a process launched by an unrestricted agent under the same
desktop OS account is a different threat model. The implementation uses ordinary SSH
configuration and an optional identity-file path. It does not create the separate OS
identity, root-managed containers, protected secret storage, or independent approval
channel required by our design. [README SSH configuration](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/README.md#ssh-configuration-recommended),
[SSH configuration object](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/config.py)

The SSH builder sets `BatchMode=yes`, `NumberOfPasswordPrompts=0`, and `ControlMaster=no`;
it has no explicit master-socket lifecycle, protected interactive login, or fail-closed
master-only operation mode. Its unit test explicitly checks that no local control socket
is required. An inherited `ControlPath` might still permit reuse: `ControlMaster=no`
alone does not disable OpenSSH's socket-reuse semantics. Consequently, compatibility
with a pre-established session is possible but **not a verified university MFA solution**.
Expiry and fallback-authentication races require new implementation and integration tests.
[SSH implementation](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/ssh/manager.py#L94),
[transport test](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/tests/unit/test_ssh_manager.py),
[OpenSSH multiplexing semantics](https://man.openbsd.org/ssh_config#ControlMaster)

Do not import the ordinary desktop SSH configuration into our secret-bearing connector
unchanged. Host aliases can refer to executable proxy/local-command settings. Pin reviewed
configuration and disable agent forwarding independently of upstream defaults.

### Job preparation, scheduling, and accounting

The Slurm manager generates a wrapper and submits via `sbatch --parsable` on stdin within
one submit operation. It can read selected directives from a mutable `.sh` file and then
launch that file with Bash. There is no durable separate prepared-package API, content
digest approval, or denial-comment workflow. Reuse requires splitting preparation from
execution and freezing source files. Its accounting result lacks allocated GPU TRES and
restart history needed for our ledger. [Submission/rendering/accounting source](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/slurm/manager.py)

Upstream resource ceilings and active-job limits are valuable request checks, not a
cumulative compute-hours allowance. No shared transactional project reservation/settlement
model was found. Its submission lock is process-local; registry locking protects metadata
writes, not a global check-plus-submit transaction. The policy must be adapted for actual
site allocations, explicit memory, GPU types, account/QoS, and whole-node rounding.
[Resource policy](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/security/slurm_policy.py),
[submission lock](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/slurm/manager.py#L26)

The upstream topology tool is described as read-only but can submit a small compute-node
probe when its cache is absent, expired, or refreshed. In our service that probe is an
execution attempt requiring approval or budget admission; capability discovery must not
silently spend resources. [Tool annotation](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/tools/registry.py#L878),
[probe submission](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/cluster/topology.py#L848)

### Persistence and ambiguous submission

The JSON registry persists remotely. Ownership defaults to a new random session ID, but
an explicitly configured stable `job_owner_id` supports process restarts. This is genuine
limited persistence; describing the project as purely ephemeral would be inaccurate.
It remains a remote logical namespace, not a central multi-user job/approval/credit ledger.
[Tracker initialization](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/slurm/jobs.py#L33),
[stable-identity tests](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/tests/unit/test_job_owner_identity.py)

Submission registers ownership after scheduler acceptance. Known registration failure
triggers a cancellation attempt; failure to cancel is reported. There is no durable
broker attempt intent and reconciliation protocol for losing the SSH response after
acceptance. Those cancellation paths cannot guarantee that an already-started job used
no compute. Retain our `submission_uncertain`, idempotency, and reserved-credit semantics.
[Registration-failure handling](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/slurm/manager.py#L226)

The tracker releases a concurrency slot when both queue and accounting queries succeed
without finding a job. That behavior must not be reused as evidence of zero compute
consumption: accounting may be delayed, incomplete, or purged. Our budget reservation
remains pending until sufficient evidence or an explicit conservative settlement.
[Active-count implementation](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/slurm/jobs.py#L360)

### Shell/filesystem tools and authority storage

`hpc.shell.run_safe` is not an unrestricted shell endpoint. It parses an allowlist,
rejects shell operators and compute programs, constrains operands, and scrubs the remote
command environment. Internal services retain a raw-command primitive for trusted
wrappers. This distinction matters: we should not equate its safe-shell API with the
arbitrary shell tool in the other Slurm MCP candidate. Our narrower public operation
set still avoids importing this extra command-policy surface.
[Restricted execution](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/shell/safe_exec.py),
[command policy](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/security/command_policy.py)

The file API includes write, upload, download, and recursive deletion below its configured
root. Path canonicalization is valuable, but canonicalize-then-use checks do not eliminate
same-UID races. The architecture document acknowledges this limitation. Our fd-relative,
no-follow helper and separation of agent artifacts from authority metadata remain
required. [File service](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/filesystem/service.py),
[path policy](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/security/path_policy.py),
[documented residual risk](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/docs/ARCHITECTURE.md#deliberate-residual-risk-three-layers)

Static inspection did not find a reserved-metadata exclusion for `.hpc-mcp` in the normal
file-policy path, while the tracker trusts a JSON registry beneath that directory.
This is an integration concern requiring a regression test before relying on upstream
ownership checks. Our approvals, authenticated online execution authority, ledger, and ownership
records live outside every agent-controlled upload/write/delete namespace; a metadata
file in a remote project tree is never the source of authority.
[Path validation](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/security/path_policy.py),
[Registry authority](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/slurm/jobs.py#L309)

Another source-level concern: the dispatcher caches the original result before response
redaction, and its cache-hit branch returns the cached result directly. Do not copy that
dispatcher unchanged; ensure secret filtering applies consistently to fresh and cached
responses. This is a static inference, not a tested exploit or claim of exhaustive review.
Redaction remains defense in depth; custody prevents secret access in the first place.
[Cache/response path](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/src/hpc_mcp/server.py#L112)

## Tests examined and evidence limits

The repository contains policy, injection, traversal, symlink, transfer, ownership,
redaction, topology, and service tests. Slurm-manager tests use scripted fake SSH rather
than a Slurm installation. Tracker registry tests execute its generated shell logic
locally, exercising actual locks/rename/conflict handling; they are stronger than mocks
for that specific filesystem behavior. They do not prove institutional shared-filesystem
or scheduler behavior. [Slurm tests](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/tests/unit/test_slurm_manager.py),
[Registry tests](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/tests/unit/test_job_tracker_registry.py)

The inspected CI installs dependencies and runs pytest on Ubuntu/Python 3.10. No real
Slurm, university MFA, protected-account Docker deployment, PostgreSQL transaction, or
GPU-driver integration job was found in that workflow. Tests were read, not run, in this
assessment. [CI workflow](https://github.com/XiaozhongFang/HPC-MCP/blob/6b86158824e9fec570da269bef8ab9165a866e9a/.github/workflows/tests.yml)

## Concrete integration spike and go/no-go criteria

Run a short implementation spike only after the broker's contracts and fake backend
exist. This assessment does not import or vendor upstream code yet.

1. Pin the reviewed commit or a deliberately selected release. Inventory license notices,
   dependencies, interpreter/remote utility requirements, and unsupported features.
2. Adapt a minimal backend package exposing prepare, stage, submit, observe, cancel, usage,
   and bounded results. Disable the upstream MCP server and broad file/shell mutation APIs
   in this integration; only our leased connector operations reach the adapter.
3. Reuse pure parsers/validators and test vectors where semantics agree. Replace global
   authorization with our control service. Replace the remote job registry as an authority
   source with our durable attempt records; it may at most supply supplementary evidence.
4. Freeze all source/wrapper bytes before approval; use explicit site profiles, environment
   digests, maximum allocation, native attempt markers, and no-requeue policy. Add required
   accounting fields and conservative finality logic.
5. Supply our protected SSH configuration, identity, master-only MFA transport, fixed
   helper operations, and safe path primitives. Never inherit local agent-readable keys
   or unreviewed desktop SSH execution hooks.
6. Run adapter conformance and fault tests: timeout after acceptance, lease expiry, duplicate
   workers, script changes after review, accounting lag, retained reservations, cross-project
   paths, reserved metadata mutations, redaction on cache hits, and topology spending.
7. Validate key-only CPU/GPU submissions and cancellation on the internal cluster, then
   university MFA connection/expiry/reconnection with tiny human-approved workloads.

**Go:** selected code reduces maintenance and passes the same contracts as a native
implementation without bypassing protected authorization or changing approved execution.
Retain upstream attribution and document local changes; upstream generally useful fixes
can be proposed separately if authorized.

**No-go as a whole-service dependency:** core guarantees require continually monkey-patching
private submit/tracker/dispatcher internals, cannot preserve frozen execution, cannot keep
all compute paths behind admission, or cannot meet master-only MFA behavior. In that case,
reuse permitted test cases and small reviewed utilities while keeping the thin first-party
adapter. Neither outcome changes the broker's public API or budget authority.
