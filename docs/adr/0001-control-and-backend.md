# Control authority and execution backend

Status: proposed. Date: 2026-10-21.

The application must enforce the same approvals and project allowances for different AI clients and later different schedulers. Existing HPC gateways/workflow tools provide execution machinery, but their client-visible controls and credential assumptions differ from this requirement.

Build one Django control service for immutable review, permissions, reservations and durable history. Put a narrow backend protocol behind its protected worker. Start with Slurm over OpenSSH, prioritizing a source audit/adaptation spike of [HPC-MCP](../references/hpc-mcp.md). Borrow AiiDA's separation of transport and scheduler. Evaluate FirecREST as an alternative Slurm gateway after authentication and exact-script tests pass; do not make it a required personal-installation dependency.

Use composition or narrowly attributed code reuse before maintaining a full upstream fork. No upstream MCP server receives a direct agent-facing privileged path around our control service. Backend choice cannot weaken review or ledger invariants. Adopting a full scientific workflow platform remains possible if provenance/workflows become a primary product goal.
