"""Export broker evidence together with locally verified delivery receipts."""

from datetime import datetime, timezone
import json
import os
import re
from uuid import UUID, uuid4

from .mcp.setup import _check_path, _ensure_directory, _parent_descriptor
from .workspace import _directory, _digest, _encoded, _logical, _read_published

MAX_MANIFEST_BYTES = 32 * 1024 * 1024


def _job_manifest(facade, identifier):
    for retry in range(2):
        cursor, records, token, result = 0, [], None, None
        while True:
            page = facade.get_job_completion(identifier, cursor, 20)
            if not isinstance(page, dict) or page.get("job", {}).get("id") != identifier:
                raise ValueError("Invalid job manifest identity")
            current = page.get("snapshot_revision")
            if not isinstance(current, str) or not re.fullmatch(r"[0-9a-f]{64}", current):
                raise ValueError("Missing job snapshot revision")
            if token is not None and token != current:
                break
            token, result = current, page
            if not isinstance(page.get("attempts"), list):
                raise ValueError("Invalid manifest attempts")
            records.extend(page["attempts"])
            if len(records) > 10000 or len(_encoded(records)) > MAX_MANIFEST_BYTES:
                raise ValueError("Job manifest exceeds export bounds")
            following = page.get("next_cursor")
            if following is None:
                check = facade.get_job_completion(identifier, 0, 1)
                if check.get("snapshot_revision") == token:
                    return {**result, "attempts": records, "next_cursor": None}
                break
            if type(following) is not int or following <= cursor:
                raise ValueError("Invalid manifest cursor")
            cursor = following
    raise ValueError("Job changed during export; retry after a bounded status wait")


def _publish(directory, document, *, resume):
    binding = {key: document[key] for key in ("schema_version", "broker_url", "project_id", "subject")}
    try:
        prior = json.loads(_read_published(directory, "run-manifest.json", MAX_MANIFEST_BYTES))
    except FileNotFoundError:
        prior = None
    if prior is not None:
        if not resume or not isinstance(prior, dict) or {key: prior.get(key) for key in binding} != binding:
            raise ValueError("Existing manifest does not belong to this export")
        info = os.stat("run-manifest.json", dir_fd=directory, follow_symlinks=False)
        if info.st_nlink != 1:
            raise ValueError("Manifest has unexpected links")
    content = _encoded(document)
    if len(content) > MAX_MANIFEST_BYTES:
        raise ValueError("Investigation manifest exceeds its metadata bound; export smaller groups")
    temporary = ".relay-manifest-" + uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, "run-manifest.json", src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
    return _digest(content)


def finish(workspace, *, job_id=None, investigation_id=None, output, resume=False, download=True, acknowledge=False):
    if (job_id is None) == (investigation_id is None):
        raise ValueError("Choose exactly one job or investigation")
    identifier = str(UUID(job_id or investigation_id))
    subject = {"kind": "job" if job_id else "investigation", "id": identifier}
    output = _logical(output)
    facade = workspace._facade()
    group = facade.get_investigation(identifier) if investigation_id else None
    identifiers = [item["id"] for item in group["jobs"]] if group else [identifier]
    if len(identifiers) > 128 or len(set(identifiers)) != len(identifiers):
        raise ValueError("Invalid investigation membership")
    destination = workspace.root / output
    _check_path(destination, directory=True)
    if destination.exists() and not resume:
        raise ValueError("Choose a new manifest directory or use --resume for this export")
    _ensure_directory(destination.parent, [])
    with _parent_descriptor(destination) as parent:
        with _directory(parent, destination.name, create=True) as folder:
            # Bind resumed artifact writes before touching an existing directory.
            plan = {"schema_version": "1.0", "broker_url": workspace.broker_url, "project_id": facade.project_id, "subject": subject}
            from .workspace import _write_relative
            _write_relative(folder, ".compute-relay-export.json", _encoded(plan), accept_identical=resume)
            jobs, verified = [], {}
            for current_id in identifiers:
                item = _job_manifest(facade, str(UUID(current_id)))
                for attempt in item["attempts"]:
                    attempt_id = str(UUID(attempt["id"]))
                    artifacts = attempt.get("artifacts")
                    if download and artifacts:
                        fingerprint = _digest(_encoded(artifacts))[:16]
                        relative = output + "/" + attempt_id + "/" + fingerprint
                        fetched = workspace.fetch(attempt_id, relative, resume=resume)
                        with _parent_descriptor(workspace.root / fetched["receipt"]) as receipt_parent:
                            receipt = json.loads(_read_published(receipt_parent, ".compute-relay-receipt.json", 1024 * 1024))
                        if (receipt.get("attempt_id") != attempt_id or receipt.get("integrity") != "verified"
                                or receipt.get("files") != artifacts):
                            raise ValueError("Downloaded inventory differs from the manifest; resume a fresh snapshot")
                        verified[attempt_id] = {"state": "verified", "receipt": fetched["receipt"], "files": artifacts}
                    elif download and artifacts == [] and (not item["package"]["declared_outputs"] or attempt["state"] != "succeeded"):
                        verified[attempt_id] = {"state": "not_applicable", "files": []}
                    else:
                        verified[attempt_id] = {"state": "not_downloaded", "files": []}
                    complete = (attempt["state"] in {"succeeded", "failed", "cancelled", "expired"}
                        and attempt.get("accounting", {}).get("final") is True
                        and attempt.get("collection_state") not in {"retry_pending", "failed"}
                        and (attempt["state"] != "succeeded" or not attempt.get("missing_outputs")))
                    if acknowledge and complete and verified[attempt_id]["state"] in {"verified", "not_applicable"}:
                        facade.acknowledge_job_results(attempt_id)
                # Record acknowledgement and concurrent progress separately from
                # the earlier immutable local receipt. New artifacts stay unverified.
                current = _job_manifest(facade, str(UUID(current_id)))
                for attempt in current["attempts"]:
                    local = verified.get(attempt["id"], {"state": "not_downloaded", "files": []})
                    if local["files"] != attempt.get("artifacts"):
                        local = {"state": "not_downloaded", "files": []}
                    attempt["local_delivery"] = local
                jobs.append(current)
            if group is not None:
                group = facade.get_investigation(identifier)
            exported_revisions = {item["job"]["id"]: item["snapshot_revision"] for item in jobs}
            pending_exports = sorted(item["id"] for item in group["jobs"]
                if exported_revisions.get(item["id"]) != item["snapshot_revision"]) if group is not None else []
            manifest = {**plan, "generated_at": datetime.now(timezone.utc).isoformat(),
                "snapshot_consistency": "per_job_revisions; concurrent changes may require another export",
                "investigation": group, "jobs": jobs, "pending_job_exports": pending_exports,
                "scientific_validation": "not_checked_by_exporter",
                "summary": {"operationally_reconciled": not pending_exports and all(item["summary"]["operationally_reconciled"] for item in jobs),
                    "local_delivery_complete": not pending_exports and all(attempt["local_delivery"]["state"] in {"verified", "not_applicable"}
                        for item in jobs for attempt in item["attempts"]),
                    "spent": group["summary"]["spent"] if group is not None else {key: sum(item["summary"]["spent"][key] for item in jobs)
                        for key in ("cpu_core_seconds", "gpu_device_seconds")},
                    "reserved": group["summary"]["reserved"] if group is not None else {key: sum(item["summary"]["reserved"][key] for item in jobs)
                        for key in ("cpu_core_seconds", "gpu_device_seconds")}}}
            digest = _publish(folder, manifest, resume=resume)
    return {"subject": subject, "manifest": output + "/run-manifest.json", "sha256": digest,
        "summary": manifest["summary"], "scientific_validation": "not_checked_by_exporter"}
