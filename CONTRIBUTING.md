# Contributing

The project is a development alpha. Start with [implemented runtime scope](docs/runtime.md), [implementation milestones](docs/implementation.md), [decision records](docs/adr/README.md), and the [security model](docs/security.md). Preserve the distinction between current behavior and target requirements.

Run the checks described in [README](README.md). Add a negative or regression case when changing a contract or a security-relevant behavior. Runtime contributions should reference the relevant stable test IDs in [the testing plan](docs/testing.md), include migrations/upgrade notes where relevant, and document site assumptions.

Do not commit credentials, institutional configuration, real cluster output, datasets, personal paths, databases, or generated runtime state. Use synthetic fixtures and attribute copied/adapted upstream code with its original license notices. Public PR checks must never contact a real cluster, consume institutional resources or run with deployment credentials.

Repository ownership, contribution licensing and the redistribution license must be established before external contributions are accepted for a public release. No CLA, DCO or license agreement is implied by this blueprint. Maintainers should choose and document a consistent contribution policy before publication.

For security issues, follow [SECURITY.md](SECURITY.md) and avoid publishing sensitive details in an ordinary issue.
