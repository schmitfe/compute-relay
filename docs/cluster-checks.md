# Cluster tests through normal job review

The cluster's **Test** page prepares a small Python diagnostic. It does not run
`sbatch` directly, open SSH from the web service or waive the normal compute
budget. Only the connection's owner can use it, through a current human session
with CSRF protection. Administrator access to another user's cluster does not
permit a test under that user's HPC identity.

First complete and confirm the cluster configuration, with an enabled connection
and current owned worker. Resource ceilings and all three finite termination,
overtime and cleanup bounds must already be explicit. Discovery alone does not
provide those timing guarantees. See [cluster configuration](cluster-configuration.md).

Open the test page, choose a permitted partition/account/job QoS and a Python
environment, then select **Prepare test for review**. The usual review page
shows the immutable source, generated batch script, environment, connection
revision, reservation and one execution request. Approve or reject there. Merely
opening the page, preparing or rejecting a test never submits an allocation.
Rejection comments remain in the normal job history.

The fixed stdlib script sleeps for two seconds, prints its hostname and Python
version, selected Python executable and loaded module names, and writes
`cluster-check.json` containing those details and the marker
`compute-broker-cluster-check-v1`. It requests two logical CPUs, or one when the
configured ceiling is one, 128 MiB, zero GPUs and a one-minute scheduler limit.
Queueing, startup and cleanup can take longer than the script. The reservation
uses configured allocation rounding and maximum charge duration. A connection
that rounds CPU-only jobs into GPU allocations is unsuitable for this test.

An existing owned recipe can be selected at first setup. The default creates a
cluster-bound system Python reference from saved discovery when available, or
an explicitly reviewed `/usr/bin/python3` fallback. This installs nothing and
does not claim compute-node validation. If that interpreter is unavailable on
compute nodes, the result is an ordinary failed job. Later recipe choices must
already be permitted by the existing diagnostic project; this page does not
silently broaden its environment allowlist.

## Finite diagnostic allowance

The first valid preparation creates one dedicated project per connection, with:

- 3,600 CPU-core seconds and no GPU allowance.
- One active job and 20 lifetime admitted execution attempts.
- Mandatory human approval for the project and its internal credential.
- A 30-day admission/credential expiry.

The credential is owner-bound and stored only as a hash; its random token is
discarded rather than shown to a browser or agent. No machine route is added
for initiating a cluster test. Ordinary project permissions, immutable review,
online dispatch confirmation, worker journal, cancellation and final accounting
still apply. Rejected preparation does not consume an execution slot; failed or
cancelled admitted jobs retain their usual lifetime-slot semantics.

Tests never replenish funds, reset counters, renew expiry, replace a revoked
credential or change an existing project back to a preferred policy. If its
configuration is paused, expired or no longer requires approval, resolve that
existing project explicitly. A large conservative reservation that cannot fit
the initial allowance is refused; the test does not silently enlarge its budget.
Normal admission still enforces current policy at approval and dispatch.

The preparation form carries a submission UUID and expected connection revision.
Replaying the same submitted form returns the same job and execution request.
Reusing that UUID with different selection bytes conflicts. Changed cluster
configuration requires a fresh preview. Source and generated-file storage run
outside the outer idempotency transactions so upload reservations remain durable.

## Results and acceptance scope

Return to the cluster test page for recent review links, bounded escaped logs,
accounting state and hash-verified `cluster-check.json` previews. The page reads
already collected broker artifacts; it never fetches files directly from SSH.
The review page shows the same diagnosis before the prepared scripts: Slurm job
ID, scheduler state, exit status, reported usage and a bounded standard-error and
standard-output preview. Recognized failures have a fixed explanation and next
step; unknown failures retain the actual facts without inventing a cause. Logs
are shown only after the existing human/project authorization check, escaped as
text, and limited to 16 KiB per stream with an explicit truncation notice.
The normal job records and accounting remain authoritative. A successful tiny
test checks this selected environment/partition path; it does not verify GPU
execution, throughput, worst-case cleanup or unrestricted automatic spending.

Local tests use synthetic identities and worker reports. They verify normal
preparation, mandatory approval, rejection comments, reservation and settlement,
idempotency, owner/CSRF checks, finite limits and output escaping. No real SSH
connection or compute allocation is part of the default tests. An operator can
run the feature against a real configured cluster only through its explicit
review and approval flow.
