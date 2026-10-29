# Contributing

Start with the [documentation guide](docs/README.md),
[implemented runtime scope](docs/runtime.md), [decision records](docs/adr/README.md),
and [the security model](docs/security.md). Accepted open work is listed in
[the roadmap](docs/roadmap.md).
Preserve the distinction between current behavior and target requirements.

This is a development alpha. Changes should identify concrete behavior, its
authorization boundary and the evidence used to validate it. Use a focused
feature branch and commits that can be reviewed independently. Do not commit
local broker databases, project keys, owner credentials, cluster profiles, SSH
material or acceptance artifacts containing private host details.

## Development checks

Use a Python version listed in `[tool.compute-relay.ci].python-versions` in
`pyproject.toml` and [uv 0.13.6](https://docs.astral.sh/uv/getting-started/installation/).
The project requires that uv version. Install and run the locked test environment:

```sh
uv sync --locked --extra test --python 3.13
uv run --locked --extra test python -m pytest -q
uv run --locked --extra test python tools/check_contracts.py
uv run --locked --extra test python tools/local_smoke.py
```

`uv.lock` is shared by development, CI and disposable Docker fixtures. Change
dependencies in `pyproject.toml`, run `uv lock`, and commit both files. Use
`uv lock --upgrade-package NAME` for an intentional dependency update; `--locked`
checks fail instead of silently changing the lock. Setuptools remains the build
backend, and ordinary wheel/pip installation is supported.

The ordinary suite uses temporary databases and tiny synthetic workloads. It does not contact a real cluster.
The NumPy science calibration is optional and separate from core dependencies.

For environment, workflow or storage changes, run the corresponding disposable
Slurm acceptance overlay described in [container testing](docs/container-testing.md).
The CI matrix exercises each extension independently. Do not point fixture
scripts at personal or institutional credentials. Real-site acceptance is an
explicit operator workflow with a separately reviewed grant and job package.

## Module boundaries

New optional capabilities belong in a separate app under
`src/compute_relay/extensions/`, with its own models, migrations, validators,
worker adapter, API, portal, tests and scope metadata. Defaults remain disabled.
Follow [the lifecycle contract](docs/extensions.md) and
[the boundary checklist](docs/module-boundaries.md). Make core changes only for
generic primitives needed to preserve shared admission, review and accounting.
Prove ordinary core operation with all extensions absent. Removing or disabling
a feature must never erase durable attempts or release uncertain reservations.

An agent-facing input is not an operator setting. Never accept arbitrary import
paths, remote command templates, secret file paths or undeclared filesystem
roots from project credentials. Installers run as scheduled workloads. Remote
metadata helpers must remain bounded and fixed. Filesystem migration requires
its own capability and policy; the current workflow extension rejects artifact
edges across clusters.

## Review and publication

Include the problem, resulting behavior, validation and material limitations
in a change description. Keep schema copies and examples consistent. The alpha
requires no backward compatibility for stored state. Regenerate the
current initial migrations when the schema changes and verify fresh setup with
all bundled modules and with core only. Document any required state reset; do
not add backfills or legacy configuration readers. Public CI must need no
secrets, institutional hosts or manual MFA.
Reference the relevant stable test IDs in [the testing plan](docs/testing.md),
include a negative or regression case for security-relevant behavior, and
document site assumptions. Attribute copied or adapted upstream code with its
original license notices.

## Contribution licensing

Compute Relay is licensed under the [Apache License, Version 2.0](LICENSE).
Unless explicitly stated otherwise, contributions intentionally submitted for
inclusion in this project are provided under that license, as described in
Section 5. Contributors must have the right to submit their work and preserve
any required third-party notices. No copyright assignment is required.

Before a public package release, maintainers must complete the release,
dependency, security and deployment checks documented in the project roadmap.

For security issues, follow [SECURITY.md](SECURITY.md) and avoid publishing
sensitive details in an ordinary issue.
