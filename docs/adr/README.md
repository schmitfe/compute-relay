# Architecture decision records

Records describe implementation direction and state their individual status.
An accepted direction is not evidence of shipped functionality. Changing a
decision requires updating its rationale, affected contracts and tests.

| Record | Decision |
| --- | --- |
| [0001](0001-control-and-backend.md) | Own the policy authority and compose a replaceable execution backend |
| [0002](0002-protected-personal-installation.md) | Single-user account boundary; stronger isolation remains outside the package |
| [0003](0003-durable-budget-and-dispatch.md) | Transactional reservations and conservative external-operation recovery |
| [0004](0004-limited-first-release.md) | Bound the first Slurm release and defer persistent runtimes |
| [0005](0005-hosted-identity-and-connectors.md) | Archived shared-server identity/database direction; superseded by the single-user package |
