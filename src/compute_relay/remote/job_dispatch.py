"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import os
import pwd
import re

from .collection import collect, output_transfer, read_logs
from .jobs import own_record, reconcile, submission_time_matches, submit, stage_input
from .protocol import JOB_OPERATIONS, JOB_PROTOCOL
from .scheduler import scheduler
from .status import observe

def execute_jobs(config, request):
    op = request.get("operation")
    if request.get("protocol") != JOB_PROTOCOL or op not in JOB_OPERATIONS:
        raise ValueError("Unsupported versioned job operation")
    if op == "submit":
        return submit(config, request)
    if op == "reconcile":
        return reconcile(config, request)
    if op == "stage_input":
        return stage_input(config, request)
    if op == "observe":
        return observe(config, request)
    identity = request["native_identity"]
    if not re.fullmatch(r"[0-9]{1,20}", identity.get("job_id", "")):
        raise ValueError("Invalid native job ID")
    record = own_record(config, identity, request.get("manifest_sha256"))
    if op in {"output_manifest", "output_chunk"}:
        return output_transfer(config, request, record)
    if op == "logs":
        return read_logs(config, request, record)
    job_id = identity["job_id"]
    if op == "cancel":
        live = scheduler(config, "squeue", ["-h", "-j", job_id, "-o", "%i|%k|%V"])
        rows = [line.split("|") for line in live["stdout"].splitlines() if line.strip()]
        marker = "broker-attempt:" + identity["attempt_id"]
        if live["returncode"] or not rows:
            # Empty squeue is not completion or nonacceptance evidence. Keep
            # cancellation pending until trustworthy observation/accounting.
            return {"outcome": "unknown"}
        if len(rows) != 1 or len(rows[0]) != 3 or rows[0][0] != job_id or rows[0][1] != marker or not submission_time_matches(rows[0][2], record["created_at"]):
            return {"outcome": "identity_mismatch"}
        user = pwd.getpwuid(os.getuid()).pw_name
        result = scheduler(config, "scancel", ["--user=" + user, "--name=" + record["manifest"]["request"]["name"], "--", job_id])
        return {"outcome": "cancel_requested" if result["returncode"] == 0 else "unknown"}
    if op == "collect":
        return collect(config, request, identity, record)
    raise ValueError("Unsupported fixed helper operation")
