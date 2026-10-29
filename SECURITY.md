# Security reporting and support

Compute Relay is a development alpha with a local portal and personal SSH
connectors. It includes project-scoped agent keys, reviewed execution,
constrained Slurm operations and compute/storage admission controls. It does not
isolate credentials from another process with the same local account's access.
See the
[security boundaries](docs/security.md) and [runtime limits](docs/runtime.md).

Report vulnerabilities through [GitHub private vulnerability reporting](https://github.com/schmitfe/compute-relay/security/advisories/new).
This repository's reporting endpoint was verified enabled on 2026-10-29.
Do not post credentials or sensitive institutional details in a public issue.
If GitHub reporting is unavailable, use an existing trusted private channel to
the repository owner; there is no public email address designated for reports.

The repository owner, [@schmitfe](https://github.com/schmitfe), is responsible
for initial triage, coordinating fixes and disclosure. Response is best effort
during the alpha; there is no guaranteed acknowledgement or resolution time.

## Supported versions and installations

Only the current `main` revision receives alpha fixes. Earlier alpha commits
and separate historical release branches are not maintained. Update to the
current revision when reproducing a report, preserving outstanding-job evidence
and following the [fresh-setup policy](docs/deployment.md#alpha-updates-and-fresh-setup).
Restart and crash recovery within one installation version remain supported.

The tested scope is standard CPython on Linux using SQLite, the local Waitress
portal and system OpenSSH. Python versions are declared once in
`[tool.compute-relay.ci].python-versions` in [pyproject.toml](pyproject.toml)
and each runs the full CI matrix. Free-threaded Python, other operating systems
and site-specific Slurm behavior are not established by that matrix. Real-site
support requires recorded acceptance for its authentication, accounting and
filesystem settings. See [testing guidance](docs/testing.md#real-cluster-acceptance).

A supported release must name its supported revision/version range and target
classes, pass their acceptance checks and retain a working private reporting
channel. Publishing this alpha policy does not declare a stable release.

Reports should identify the affected version/commit, installation settings and
violated boundary, with a minimal synthetic reproduction. Do not probe real
clusters, access another person's data, launch jobs or extract live secrets
without the operator's explicit authorization.

The automated security and recovery checks are described in
[testing guidance](docs/testing.md). They are not a complete external security
audit. Host-administrator or Docker-daemon access, same-account credential
access, and hostile jobs sharing a remote cluster UID remain outside the
isolation the local package provides.
