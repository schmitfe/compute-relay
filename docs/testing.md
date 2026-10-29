# Testing the local package

Default checks use temporary directories and synthetic identities. They do
not connect to an institutional cluster. Run from a source checkout with a
CI-tested Python version (shown in the README badge) and
[uv 0.13.6](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv sync --locked --extra test --python 3.13
uv run --locked --extra test python -m pytest -q
uv run --locked --extra test python tools/check_contracts.py
uv run --locked --extra test python -m unittest discover -s tests/contracts -v
uv run --locked --extra test python tools/local_smoke.py
uv run --locked --extra test python tools/local_smoke.py --mode budgeted
uv run --locked --extra test python tools/local_smoke.py --mode budgeted --mcp-jobs 100
uv run --locked --extra test python tools/package_smoke.py
```

## Layers and evidence

| Layer | What it verifies |
| --- | --- |
| Contracts | Schemas, examples, canonical manifests and documented bounds |
| Unit and HTTP integration | Local owner authority, project scopes, review, CSRF, policy, job state and bounded output |
| SQLite transactions | Durable reservations/holds, lifetime caps, settlement uniqueness, process races and restart recovery |
| Worker and SSH fixtures | Fixed arguments, no fallback login, time/output limits, journal replay, uncertain submission and native identity checks |
| Module suites | Enabled/disabled boundaries, storage estimates, scheduled environment publication and workflow materialization |
| Investigation completion | Finite typed plans, grouped accounting, scope closeout, revision waits, safe manifest export and verified local receipts |
| Native local smoke | A real loopback HTTP service with synthetic jobs and an idle/fake worker |
| Installed distribution | Wheel and sdist completeness, clean-venv plain install, runtime imports, HTML templates, launcher start, Ctrl-C and same-state restart |

`tools/package_smoke.py` copies only public source/package inputs into a temporary
build directory. It builds both distributions, verifies runtime resource coverage,
installs the wheel with no extras into a clean environment, checks dependencies
and starts the installed launcher with private disposable state and no
connectors. It uses loopback HTTP and restarts the same state directory. It may
need registry access for build/runtime packages. `--skip-launch` is available
only for diagnosing packaging; that option does not establish launcher acceptance.

The fake backend never executes uploaded code in the portal or connector.
The bounded MCP burst starts the real stdio MCP server and four-thread Waitress
portal, prepares up to 100 distinct jobs with stable-key replays, executes them
and verifies every output digest and exact settled accounting. Concurrent health
and authenticated portal probes must all succeed. It prints aggregate counts
and latency rather than committing a benchmark report or fixture credentials.
CI includes this burst in its existing runtime workflow.
Passing synthetic tests does not establish actual Slurm behavior or operating-
system isolation. SQLite uses immediate write transactions; admission and
settlement correctness must not depend solely on a process-local mutex.

## CI

CI reads its Python matrix from `[tool.compute-relay.ci].python-versions` in
`pyproject.toml`. The README Python badge reads that same list from `main`;
the CI badge shows the combined runtime workflow status on `main`. To add or
remove a tested Python version, edit that list and let the full matrix pass
before merging. `project.requires-python` separately sets the installation
compatibility floor; it does not claim every newer interpreter has been tested.

The matrix runs on Linux with uv 0.13.6, a pinned setup action and a
dependency cache keyed by the lock. The runtime workflow installs the test extra
with `uv sync --locked --extra test` and the committed `uv.lock`; a stale lock
fails the build. The runtime workflow checks SQLite and contracts, verifies
bundled migrations, exercises synthetic workflows, and performs clean-wheel
acceptance. Planning contracts are collected by the same pytest run. The
disposable application images install
locked runtime dependencies without test extras or an editable source install.

The manual `Optional disposable Slurm acceptance` workflow builds a synthetic
SSH/Slurm/accounting cluster and runs the personal-connector smoke test. See
[container testing](container-testing.md). Its credentials and data are generated
for the fixture and are removed afterward. It never accesses a real site.

## Real-cluster acceptance

Real clusters are opt-in and outside normal CI. Use a reviewed test project,
account/partition and tiny finite resource limits. Confirm the actual compute
node can find the interpreter/modules, review the exact batch/script content,
then check native accounting, output hashes and zero residual reservation.
Queue presence or cancellation acknowledgement alone is not settlement proof.
Test SSH MFA with human participation; never place institutional credentials in
public CI or run untrusted pull-request code with them.

The separate [investigation acceptance](investigations.md#verification) runs one
reviewed job on the disposable Slurm fixture, closes unused packages/reviews,
and exercises the installed export CLI, resume, hashes and shared accounting.

Fresh database creation is checked with core only and with the bundled modules.
Historical alpha schema migrations are unsupported; same-version restart and
crash recovery still preserve job and budget history.
