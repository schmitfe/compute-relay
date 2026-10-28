# Control authority and execution backend

Status: accepted. Date: 2026-10-21.

The application must enforce the same approvals and project allowances for different AI clients and later different schedulers. Existing HPC gateways/workflow tools provide execution machinery, but their client-visible controls and credential assumptions differ from this requirement.

Use one local control service for immutable review, permissions, reservations and durable history. Keep a narrow backend protocol behind its personal connector. The implementation starts with Slurm over OpenSSH. Reuse suitable libraries only after a focused source and license review; no upstream MCP server gets a privileged path around control. Alternative gateways require authentication, exact-script and accounting acceptance before adoption.

Use composition or narrowly attributed code reuse before maintaining a full upstream fork. No upstream MCP server receives a direct agent-facing privileged path around our control service. Backend choice cannot weaken review or ledger invariants. Adopting a full scientific workflow platform remains possible if provenance/workflows become a primary product goal.
