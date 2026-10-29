# Documentation guide

Start here when maintaining Compute Relay. This guide describes the current
package and links to its operational and development documentation.

| Need | Read |
| --- | --- |
| Install, recover or upgrade | [Deployment](deployment.md) |
| Understand shipped behavior and limits | [Runtime](runtime.md) |
| Configure clusters and projects | [Portal](management.md), [connectors](personal-connectors.md), [cluster setup](cluster-configuration.md) |
| Connect a science project | [Local project and client setup](local-client-setup.md) |
| Orchestrate jobs in a local agent script | [Python SDK](python-sdk.md) |
| Understand authority and trust boundaries | [Architecture](architecture.md), [security](security.md) |
| Review API and planning contracts | [API target contract](api.md), [schemas](../contracts/README.md) |
| Run checks | [Testing](testing.md), [disposable Slurm testing](container-testing.md) |
| Understand modules | [Extensions](extensions.md), [environments](environment-builds.md), [workflows](workflows.md), [investigations](investigations.md) |
| See remaining work | [Prioritized roadmap](roadmap.md) |

`docs/adr/` records durable design decisions. `docs/references/` holds source
assessments used by those decisions. The [agent integration kit](../examples/agent-integration/README.md)
contains its own prompts and acceptance rubrics.
