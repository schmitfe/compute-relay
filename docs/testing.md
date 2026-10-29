# Testing the local package

Default checks use temporary directories and synthetic identities. They do
not connect to an institutional cluster. Run from a source checkout with Python
3.12 or 3.13 and [uv 0.13.6](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv sync --locked --extra test --python 3.13
uv run --locked --extra test python -m pytest -q
uv run --locked --extra test python tools/check_contracts.py
uv run --locked --extra test python -m unittest discover -s tests/contracts -v
uv run --locked --extra test python tools/local_smoke.py
uv run --locked --extra test python tools/local_smoke.py --mode budgeted
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
Passing synthetic tests does not establish actual Slurm behavior or operating-
system isolation. SQLite uses immediate write transactions; admission and
settlement correctness must not depend solely on a process-local mutex.

## CI

CI runs Python 3.12/3.13 on Linux with uv 0.13.6, a pinned setup action and a
dependency cache keyed by the lock. Both Python workflows install the test extra
with `uv sync --locked --extra test` and the committed `uv.lock`; a stale lock
fails the build. The runtime workflow checks SQLite and contracts, verifies
bundled migrations, exercises synthetic
workflows, and performs clean-wheel acceptance. Planning contracts also run
independently with the same lock. The disposable application images install
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

Schema migrations preserving job and budget history are covered by the suite.
