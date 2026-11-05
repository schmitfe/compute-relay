# Contributing

Use focused changes with concrete behavior, validation and limitations. Check the
[roadmap](docs/roadmap.md) before expanding scope. Never commit broker state,
credentials, cluster profiles or private acceptance evidence. Report security
issues through [SECURITY.md](SECURITY.md).

## Development

Use the Python matrix in `pyproject.toml` and the pinned uv version:

```sh
uv sync --locked --extra test --python 3.13
uv run --locked --extra test python -m pytest -q
uv run --locked --extra test python tools/check_contracts.py
uv run --locked --extra test python tools/local_smoke.py
```

Change dependencies in `pyproject.toml`, run `uv lock`, and commit both files.
Use `uv lock --upgrade-package NAME` for deliberate upgrades. The lock serves
development, CI and disposable fixtures; ordinary wheel installation remains
supported. See [testing](docs/testing.md) for package and native acceptance checks.
Fixture credentials are synthetic; real-site work needs an explicitly reviewed
grant and job package. Public CI must require no institutional secrets or MFA.

## Change requirements

Preserve [security boundaries](docs/security.md), atomic admission/accounting,
input validation and uncertain-job evidence. Project input must never choose
imports, remote commands, credential paths or undeclared filesystem roots.
Optional capabilities follow the [extension contract](docs/extensions.md),
including core-only operation and retained state when disabled.

Keep schemas/examples consistent. This alpha does not promise old stored-state
compatibility: regenerate initial migrations after schema changes, verify fresh
core-only and bundled setup, and document any required reset. Do not add legacy
readers or backfills. Describe validation and site assumptions; security changes
need negative/regression evidence. Preserve upstream attribution and licenses.
Before publication, complete the release gates in the testing guide.

## Contribution licensing

Contributions are provided under [Apache-2.0](LICENSE), Section 5, unless explicitly
stated otherwise. Contributors must hold the necessary rights and preserve
third-party notices. No copyright assignment is required.
