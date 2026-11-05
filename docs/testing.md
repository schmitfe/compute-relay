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

Ordinary PR/main checks run one Linux job on the lowest supported Python version.
Branch pushes use their PR check rather than duplicating it. For a release
candidate, manually run **Local package and SQLite** on its branch with
`release_candidate` enabled. Releases always enable this full matrix:
`[tool.compute-relay.ci].python-versions` in `pyproject.toml`, across Linux and
macOS 15 Apple Silicon (six jobs). Intel Macs are outside the tested support matrix.
Every matrix job must pass before publication.
That version list also supplies the README badge; `project.requires-python` sets
the install floor, not proof for untested interpreters. All jobs use the locked
test extra, pytest, HTTP/MCP, migrations and clean-wheel acceptance. Mac jobs use
system OpenSSH against a disposable loopback sshd to verify master loss, refusal
of implicit login and permitted key-only renewal. No account SSH settings change.

The [disposable Slurm workflow](container-testing.md) runs on every push to
`main`, manually and for releases. Its synthetic SSH/Slurm/accounting
site tests core recovery, environment builds, DAGs, storage and investigations
without VPN credentials. Faults include SSH expiry/death,
accepted-report reply loss, restart and interrupted collection. It checks stable
native identities, one submission, verified outputs, exact accounting, zero
reserved and responsive portal/status requests. Module guides describe the native
checks; the [runtime record](runtime.md#acceptance-evidence-and-remaining-reliability-work)
distinguishes completed fixture and real-site runs.

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
defines scope. Record evidence for each capability; passing one does not imply the others.

| Target | Required acceptance |
| --- | --- |
| Core | Green exact-candidate Python, installed-wheel, MCP and native Slurm checks; accepted site classes. The unreproduced portal stall is deferred until recurrence, with stack capture retained. |
| GPU | A permitted real job verifies device/type access, outputs, reviewed allocation, exact CPU/GPU settlement and zero reserved. Cover typed/generic requests where both are advertised. |
| Agent-created environments | Native automatic creation under owner permission, pinned-wheel import, reuse and immutable update. Recover publication after a hook failure/restart without rebuilding or charging twice. Only Python venv builders are included. |
| DAGs | Native branching/joining and artifact edges preserve per-node review, dependencies, identities, hashes and accounting; revoked keys cannot advance nodes. |
| Storage/investigations | Browse/create/enroll/check and byte/inode probes on intended filesystems; capacity stays advisory. Closeout/export/resume preserves scopes, hashes and the shared ledger. |
| macOS | Handle case-insensitive private-name aliases and case/Unicode file/ZIP collisions on default APFS. Make process tests meaningful without `/proc`; verify default paths, no-follow writes, install/restart, MCP and system OpenSSH. macOS 15 Apple Silicon is provisional for the first beta; the human browser/terminal-MFA/reconnect/shutdown walkthrough is required for `0.1.0` final. Intel is outside tested support. |

Record commit, platform/site class, checks and remaining limits in the concise
runtime evidence. Keep credentials, raw logs and agent reports outside Git.

After the gates pass:

1. Declare supported beta versions/platforms, known limits and support policy in
   `SECURITY.md` and release notes; verify private reporting. Validate separate
   service/agent UID isolation before recommending it.
2. Set `0.1.0b1` in `pyproject.toml` and `compute_relay.__version__` (health uses it);
   regenerate `uv.lock`. Update release wording, PyPI README rendering and package metadata.
   Retain fresh setup for incompatible updates and uncertain-job evidence.
3. Verify wheel/sdist installation, launch/restart and green CI for that exact
   release commit.
4. With separate owner go-ahead, tag `v0.1.0b1` and publish a GitHub prerelease.
   The release workflow below gates PyPI upload. Verify the published install;
   preparing this branch publishes no release.

## PyPI release setup

Configure a [pending trusted publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
in the owner's PyPI account: project `compute-relay`, GitHub owner `schmitfe`,
repository `compute-relay`, workflow `release.yml`, environment `pypi`. A missing
public entry does not reserve the name; the first successful upload creates it.

Create GitHub environment `pypi`, restrict it to release tags and require owner
approval. No PyPI password or API token belongs in repository secrets.

`.github/workflows/release.yml` runs only when a GitHub release is published.
It requires a tag matching both package versions on a commit contained in `main`,
and a matching prerelease flag. It reruns the shared platform and disposable
Slurm workflows; failed/cancelled checks prevent upload. The build job retains
the archives it clean-installed/restarted, and a separate minimally privileged
job publishes those exact archives using Trusted Publishing and provenance
attestations. Ordinary commits, PRs and draft releases never upload. PyPI releases
are immutable: fix a bad release with a new version rather than replacing files.
