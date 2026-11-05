# Testing

From a checkout, with [uv 0.13.6](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv sync --locked --extra test --python 3.13
uv run --locked --extra test python -m pytest -q
uv run --locked --extra test python tools/local_smoke.py
uv run --locked --extra test python tools/local_smoke.py --mode budgeted --mcp-jobs 100
uv run --locked --extra test python tools/package_smoke.py
```

These checks use disposable state and synthetic credentials, never an
institutional cluster. Fake workers do not execute uploaded workload code.

| Check | Evidence |
| --- | --- |
| Pytest | Contracts, owner/key boundaries, module enablement, lifecycle/fault cases and real SQLite admission/settlement races. Includes fresh database creation; initial migrations are required. |
| Local HTTP/MCP smoke | Reviewed and budgeted jobs through the real service; the 100-job stdio MCP burst checks stable-key replays, output hashes, exact accounting and concurrent portal responses. |
| Package smoke | Wheel/sdist resources, plain clean install, dependencies, HTML, launcher shutdown and same-state restart; may need package-index access. `--skip-launch` omits lifecycle acceptance. |

For contracts alone, run `python tools/check_contracts.py` or
`python -m unittest discover -s tests/contracts -v`; pytest already includes them.
Prefer a small behavior test at the changed boundary. Share setup where it
removes duplication, retain distinct authorization/fault cases, and avoid
assertions that only mirror implementation structure.

## CI

`[tool.compute-relay.ci].python-versions` in `pyproject.toml` defines the Linux
matrix and README version badge. Update that list and require every version to
pass. `project.requires-python` sets the install floor, not proof for untested
newer interpreters. CI uses the locked test extra, checks bundled migrations,
runs pytest/HTTP/MCP checks and clean-wheel acceptance.

The [disposable Slurm workflow](container-testing.md) runs on every push to
`main` and manually. Its synthetic SSH/Slurm/accounting site tests core recovery
and investigations without VPN credentials. Faults include SSH expiry/death,
accepted-report reply loss, restart and interrupted collection. It checks stable
native identities, one submission, verified outputs, exact accounting, zero
reserved and responsive portal/status requests. Optional module guides contain
additional native smoke commands; their presence is not a recorded pass.

## Real-cluster acceptance

Use an approved project, partition/account and tiny finite resource limits.
Review the exact package and verify the interpreter/modules on a compute node,
output hashes, actual allocated usage and zero residual reservation. Queue
presence or cancellation acknowledgement alone is insufficient. Exercise the
advertised authentication/accounting/filesystem classes, with human MFA where
needed. Never put institutional credentials in public CI or run untrusted PR
code against them. Synthetic success does not prove native/site behavior.

## First beta release gates

Plan `0.1.0b1`, then `0.1.0` final. The [roadmap](roadmap.md#planned-first-beta)
defines scope; current Linux CPU evidence does not establish GPU or Mac support.

| Target | Required acceptance |
| --- | --- |
| Core | Green exact-candidate Python, installed-wheel, MCP and native Slurm checks; accepted site classes. Resolve the P1 portal-stall work or explicitly reassess its release impact with captured evidence. |
| GPU | A permitted real job verifies device/type access, outputs, reviewed allocation, exact CPU/GPU settlement and zero reserved. Cover typed/generic requests where both are advertised. |
| Agent-created environments | Native automatic creation under owner permission, pinned-wheel import, reuse and immutable update. Recover publication after a hook failure/restart without rebuilding or charging twice. Only Python venv builders are included. |
| DAGs | Native branching/joining and artifact edges preserve per-node review, dependencies, identities, hashes and accounting; revoked keys cannot advance nodes. |
| Storage/investigations | Browse/create/enroll/check and byte/inode probes on intended filesystems; capacity stays advisory. Closeout/export/resume preserves scopes, hashes and the shared ledger. |
| macOS | Handle case-insensitive private-name aliases and case/Unicode file/ZIP collisions on default APFS. Make process tests meaningful without `/proc`; verify default paths, no-follow writes, install/restart, MCP, browser launch, system OpenSSH, MFA, reconnect and shutdown. Name tested OS versions/architectures. |

Record commit, platform/site class, checks and remaining limits in the concise
runtime evidence. Keep credentials, raw logs and agent reports outside Git.

After the gates pass:

1. Declare supported beta versions/platforms, known limits and support policy in
   `SECURITY.md` and release notes; verify private reporting. Validate separate
   service/agent UID isolation before recommending it.
2. Set `0.1.0b1` in `pyproject.toml`, `compute_relay.__version__` and the health
   response; regenerate `uv.lock`. Update alpha wording and package metadata.
   Retain fresh setup for incompatible updates and uncertain-job evidence.
3. Verify wheel/sdist installation, launch/restart and green CI for that exact
   release commit.
4. With separate owner go-ahead, tag it and create a GitHub prerelease with
   verified distributions. If using PyPI, establish package ownership/publishing
   credentials first and verify the published install. This cleanup publishes
   no release.
