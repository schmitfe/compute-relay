# Completion, recovery and export

An investigation can finish scientifically while execution or accounting still
needs reconciliation. Preserve each state rather than merging them into success:

| State | Evidence |
| --- | --- |
| Client permission review | The client reports whether its own approval completed; the broker does not observe this layer |
| Broker execution request | A recorded request and its approval state; this can hold budget without scheduler submission |
| Submission | dispatch_started records intent; native_identity confirms a scheduler receipt; an uncertain submission still needs reconciliation |
| Execution | Terminal scheduler observation; this does not establish correct science |
| Collection | Declared outputs registered as retained artifacts; recovery uses retry_job_outputs, never another compute run |
| Local delivery | Workspace helper receipt after a complete hash-verified download; byte-request tracking alone cannot prove it |
| Accounting | Final ledger charge or definitive release, with outstanding reservations visible |
| Acknowledgement | This key records that results were reviewed/recorded; it does not certify scientific validity |
| Science | The investigator's checks, findings, uncertainty and stopping decision |

After a client/tool approval timeout, inspect get_job.execution_progress before
retrying. Report the client event separately from whether a broker request exists,
dispatch started or compute_submitted is confirmed. An uncertain response is not
permission to make a second execution request. A stable default execution key
reuses the first request for that immutable job; deliberate repeats need an
explicit new key.

get_job includes completion blockers and a revision. Use after_revision with
wait_seconds from 1 through 20 to wait for a material change; a timeout returns
changed=false. Waits do not submit, collect or acknowledge work, and recheck current
authority. WAIT_BUSY means to back off rather than create more concurrent waits.
get_job_completion supplies paginated provenance, artifact descriptors, accounting
and per-key access state. Follow next_cursor for attempts and next_execution_cursor
for requests, passing their independent cursor and execution_cursor values. Compare
snapshot_revision across pages when exporting an actively changing job. Summary
counts cover the whole job; blockers contains at most 20 details, with blocker_count
and blockers_truncated describing the full set. Ordinary get_job shows the latest
bounded history, while files finish exports all completion pages.

With investigations enabled, create_investigation records the question,
deliverable, core experiments, required validation, finite compute allowance and
stopping rule. New packages can specify investigation_id and role; revisions
inherit the group. add_investigation_jobs attaches existing records. Filter
list_project_jobs by investigation_id, or use get_investigation for aggregate
charges, reservations and abandoned drafts. These plan limits guide agents; the
ordinary project grant is the transactional spending authority.

finish_investigation closes scope with sufficient, inconclusive or user_stopped
and a report. cancel_unstarted optionally cancels never-requested drafts and
pending human reviews with jobs:cancel authority. It leaves admitted attempts,
uncertain dispatch and final accounting to the ordinary cancellation/recovery
APIs. Closing scope never claims that results were downloaded or validated.

Use the project-local helper guide's exact installed command for `files finish`.
It exports a run manifest for an investigation or a single job, fetches available
retained results into per-attempt directories, verifies their hashes, and records
unresolved states. `--no-download` exports broker evidence only.
`--acknowledge` explicitly acknowledges only reconciled attempts whose required
retained outputs were verified locally. Scientific conclusions remain separate.
Keep both the manifest and local verification receipts with the final report.
