# First release execution scope

Status: accepted. Date: 2026-10-21.

Arrays, automatic requeue, elastic allocations and persistent worker pools can make both review and resource accounting ambiguous. The first release must establish a small dependable lifecycle on the two existing clusters.

Initially support single-node jobs, whole-minute scheduler limits, named environment revisions, exact file outputs and no user-controlled scheduler flags. Reject arrays, multi-node jobs, native retries/requeue requests, resizing, exclusive allocation and arbitrary installer hooks. Explicit broker retries are new authorized attempts with new reservations.

RASER is a candidate runtime inside a later approved allocation, not the broker's policy authority. Future runtimes need bounded restart/requeue behavior, allocation-level accounting, immutable runtime artifacts, task ownership fencing and side-effect recovery before enabling budgeted operation. [Assessment](../references/paper-2609-03598.md)
