"""Bounded status queries with independent journal binding for every attempt."""

import re

from .jobs import own_record
from .protocol import JOB_PROTOCOL, STATUS_BATCH_SIZE
from .scheduler import sacct_response, scheduler, scheduler_rows


def status_rows(response, job_ids, fields):
    grouped = {job_id: [] for job_id in job_ids}
    try:
        rows = scheduler_rows(response, fields)
        if any(row[0] not in grouped for row in rows):
            raise ValueError("Unexpected scheduler identity")
    except ValueError:
        return {job_id: {"returncode": 1, "stdout": "", "stderr": "Malformed scheduler records"} for job_id in job_ids}
    for row in rows:
        grouped[row[0]].append("|".join(row))
    return {job_id: {"returncode": response["returncode"],
        "stdout": "\n".join(lines), "stderr": ""} for job_id, lines in grouped.items()}


def observe(config, request):
    if request.get("operation") != "observe" or request.get("protocol") != JOB_PROTOCOL:
        raise ValueError("Unsupported status protocol")
    items = request.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= STATUS_BATCH_SIZE:
        raise ValueError("Invalid status batch size")
    results, valid = {}, {}
    for item in items:
        identity = item["native_identity"]
        attempt_id, job_id = identity["attempt_id"], identity["job_id"]
        if attempt_id in results or not isinstance(job_id, str) or not re.fullmatch(r"[0-9]{1,20}", job_id):
            raise ValueError("Invalid or duplicate status identity")
        results[attempt_id] = {"error": "IDENTITY_UNVERIFIED"}
        try:
            record = own_record(config, identity, item["manifest_sha256"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        valid[attempt_id] = (job_id, record["manifest"]["maximum_allocation"]["gpu_devices"])
    if not valid:
        return {"items": results}
    job_ids = sorted({job_id for job_id, _ in valid.values()})
    queue = status_rows(scheduler(config, "squeue", ["-h", "-j", ",".join(job_ids), "-o", "%i|%T|%k|%V"]), job_ids, 4)
    missing = [job_id for job_id in job_ids if not queue[job_id]["stdout"]]
    accounting = {}
    if missing:
        accounting = status_rows(sacct_response(scheduler(config, "sacct",
            ["--json", "-j", ",".join(missing), "-X", "-D"])), missing, 11)
    for attempt_id, (job_id, gpus) in valid.items():
        results[attempt_id] = {"queue": queue[job_id],
            "accounting": accounting.get(job_id, {"returncode": 0, "stdout": "", "stderr": ""}), "expected_gpus": gpus}
    return {"items": results}
