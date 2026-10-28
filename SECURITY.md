# Security reporting and support

Compute Relay is a development alpha with a local portal and personal SSH
connectors. It includes project-scoped agent keys, reviewed execution,
constrained Slurm operations and compute/storage admission controls. It does not
isolate credentials from another process with the same local account's access.
See the
[security boundaries](docs/security.md) and [runtime limits](docs/runtime.md).

Use the repository's private vulnerability-reporting feature if the maintainer
has enabled it. Otherwise contact the repository owner through an existing
trusted private channel. Do not post credentials or disclose sensitive
institutional details in a public issue. Maintainers should
publish a verified reporting channel, supported versions and response
responsibilities before claiming release support.

Reports should identify the affected version/commit, installation settings and
violated boundary, with a minimal synthetic reproduction. Do not probe real
clusters, access another person's data, launch jobs or extract live secrets
without the operator's explicit authorization.

The automated security and recovery checks are described in
[testing guidance](docs/testing.md). They are not a complete external security
audit. Host-administrator or Docker-daemon access, same-account credential
access, and hostile jobs sharing a remote cluster UID remain outside the
isolation the local package provides.
