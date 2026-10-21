# Security reporting and support

This repository contains a development alpha with control, worker, Slurm/SSH, and MCP code plus synthetic tests. There is no supported protected broker release. The documented host-level isolation properties remain installation requirements rather than verified guarantees; see [runtime limitations](docs/runtime.md).

Before making the repository public, maintainers must enable a private vulnerability-reporting channel, verify who receives reports, and publish supported release versions and response responsibilities. Until that channel is configured, contact the repository owner through an existing trusted private channel. Do not invent or use an unverified contact address, post credentials, or disclose sensitive institutional details in a public issue.

Reports should identify the affected version/commit, deployment profile and violated boundary, with a minimal synthetic reproduction. Do not probe real clusters, access another user's data, launch jobs, or extract live secrets without the operator's explicit authorization.

The intended supported threat model and its limits are documented in [docs/security.md](docs/security.md). Root/host-administrator compromise, an agent controlling its own Docker daemon, same-user secret files, and hostile jobs sharing a remote cluster UID are not equivalent to the protected service profile.
