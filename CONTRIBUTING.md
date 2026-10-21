# Contributing

Start with [implemented runtime scope](docs/runtime.md),
[implementation milestones](docs/implementation.md),
[decision records](docs/adr/README.md), and [the security model](docs/security.md).
Preserve the distinction between current behavior and target requirements.

This is a development alpha. Changes should identify concrete behavior, its
authorization boundary and the evidence used to validate it. Use a focused
feature branch and commits that can be reviewed independently. Do not commit
local broker databases, project keys, owner credentials, cluster profiles, SSH
material or acceptance artifacts containing private host details.

## Development checks

Use Python 3.12 or 3.13 in a virtual environment, then install the locked test
dependencies with `python -m pip install -c requirements.lock '.[test,mcp]'`.
Run `python -m pytest -q`, `python tools/check_contracts.py`, and
`python tools/local_smoke.py --management`. The ordinary suite uses temporary
databases and tiny synthetic workloads. It does not contact a real cluster.
The NumPy science calibration is optional and separate from core dependencies.

For environment, workflow or storage changes, run the corresponding disposable
Slurm acceptance overlay described in [container testing](docs/container-testing.md).
The CI matrix exercises each extension independently. Do not point fixture
scripts at personal or institutional credentials. Real-site acceptance is an
explicit operator workflow with a separately reviewed grant and job package.

## Module boundaries

New optional capabilities belong in a separate app under
`src/compute_broker/extensions/`, with its own models, migrations, validators,
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
in a change description. Keep schema copies and examples consistent. Include a
data migration when stored contracts change; do not rewrite previously released
migrations. Public CI must need no secrets, institutional hosts or manual MFA.
Reference the relevant stable test IDs in [the testing plan](docs/testing.md),
include a negative or regression case for security-relevant behavior, and
document site assumptions. Attribute copied or adapted upstream code with its
original license notices.

No public remote or release is configured in this workspace. Before public
distribution, the maintainers must choose a license and complete the release,
dependency, security and deployment checks documented in the project roadmap.
Repository ownership and contribution licensing must also be established
before external contributions are accepted. No CLA, DCO or license agreement
is implied by this blueprint.

For security issues, follow [SECURITY.md](SECURITY.md) and avoid publishing
sensitive details in an ordinary issue.
