# Durable budgets and dispatch

Status: proposed. Date: 2026-10-21.

Concurrent agents, queued jobs and connection loss make a read-then-decrement balance unsafe. Slurm submission cannot be atomically committed with a local database record.

Use integer allocated resource-seconds, conservative prepared allocation/termination bounds, transactional reservations and an append-only ledger. Commit authorization/reservation/outbox together, then perform network operations outside database locks. Workers have narrow authenticated online leases and no database authority. Signing grants is optional future defense for relays, not offline spending authority.

After a possibly accepted submission, keep funds reserved and reconcile exact native identity. Do not retry merely because a lease or SSH connection expired. Settle from final allocation evidence and record actual overages/debt rather than clamping them. Prefer delayed availability to an incorrect refund. PostgreSQL and SQLite require different, explicitly tested transaction strategies.
