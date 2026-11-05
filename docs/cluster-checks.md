# Cluster tests through normal job review

Only the connection owner may use **Test**, with a current human session/CSRF.
Finish cluster configuration first, including finite ceilings and a current worker.
Another user's administrator access does not grant that user's HPC identity.

1. Select a permitted partition/account/QoS and Python recipe, then **Prepare test
   for review**. Review exact source, batch script, environment, reservation and
   connection revision. Opening/preparing/rejecting never submits compute.
2. Approve the ordinary execution request. Return to **Test** for its native
   identity, accounting, bounded logs and verified `cluster-check.json`.

The fixed stdlib script sleeps two seconds and records hostname, Python version/
executable, modules and marker `compute-relay-cluster-check-v1`. It requests two
logical CPUs (one if capped at one), 128 MiB, zero GPUs and one minute. Allocation
rounding determines the reservation; actual usage determines settlement. A
connection rounding CPU-only requests into GPUs cannot use this test.

An owned recipe can be selected initially. The default uses discovered system
Python or explicitly reviewed `/usr/bin/python3`; it installs nothing and may fail
on compute. Later choices must already be in the diagnostic project's allowlist.
The page never silently expands that access.

## Finite diagnostic allowance

First valid preparation creates one project per connection:

| Control | Value |
| --- | --- |
| Compute | 3,600 CPU-core-seconds, no GPUs |
| Attempts | One active; 20 lifetime |
| Review | Required by project and internal key |
| Admission/key expiry | 30 days |

The owner-bound internal credential is hashed and its token discarded, never
given to a browser/agent. No machine initiation route exists. Normal admission,
journals and settlement apply. Rejected work consumes no attempt; admitted
failures/cancellations retain their lifetime charge.

Tests never replenish allowance, reset counts, renew expiry, replace revoked keys
or repair changed project policy. Resolve paused/expired/non-review configurations
explicitly. Oversized reservations fail rather than enlarging the budget.
A form UUID/revision binds retries to the same job/request; changed selections
conflict and changed cluster configuration requires a fresh preview.

## Results and acceptance scope

The page reads retained broker artifacts, never SSH files directly. It displays
native status/usage and escaped logs, at most 16 KiB per stream with truncation.
Recognized failures receive fixed guidance; unknown causes remain unknown.
Job records/accounting are authoritative. One successful selected recipe/partition
checks neither GPUs, throughput, worst-case cleanup nor unattended spending.
Default tests use synthetic identities/reports; real-site use requires explicit
review and approval.
