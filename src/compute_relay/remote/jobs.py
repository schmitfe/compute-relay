"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import base64
import datetime
import fcntl
import os
import pwd
import re
import uuid

from .filesystem import (
    canonical, components, digest, load_json, open_directory, open_regular,
    read_file, uuid_value, write_json, file_digest,
)
from .prelude import MAX_FILE_TOTAL
from .protocol import require_journal_binding, require_management_protocol
from .scheduler import scheduler

def stage(attempt_dir, manifest, encoded_files):
    entries = manifest["files"]
    if len(entries) > 110 or set(encoded_files) != {entry["path"] for entry in entries}:
        raise ValueError("Package file set mismatch")
    total = 0
    for entry in entries:
        path = entry["path"]
        parts = components(path)
        if path.startswith("/") or len(path) > 240:
            raise ValueError("Invalid staged file path")
        if encoded_files[path] is None:
            fd = open_regular(attempt_dir, path)
            try:
                if os.fstat(fd).st_size != entry["size_bytes"] or file_digest(fd) != entry["sha256"]:
                    raise ValueError("Transferred input differs from approved manifest")
            finally:
                os.close(fd)
            total += entry["size_bytes"]
            if total > MAX_FILE_TOTAL:
                raise ValueError("Package exceeds total byte limit")
            continue
        data = base64.b64decode(encoded_files[path], validate=True)
        total += len(data)
        if total > MAX_FILE_TOTAL or len(data) != entry["size_bytes"] or digest(data) != entry["sha256"]:
            raise ValueError("Staged file differs from approved manifest")
        parent_fd = open_directory("/".join(parts[:-1]), create=True, parent=attempt_dir) if len(parts) > 1 else os.dup(attempt_dir)
        try:
            try:
                existing = read_file(parent_fd, parts[-1], len(data))
            except FileNotFoundError:
                temporary = "_upload_" + uuid.uuid4().hex
                mode = 0o500 if entry["executable"] else 0o400
                fd = open_regular(parent_fd, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Atomic rename occurs inside the per-attempt broker lock.
                    # Same-UID malicious mutation remains outside this boundary.
                    os.rename(temporary, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    os.fsync(parent_fd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
            else:
                if digest(existing) != entry["sha256"]:
                    raise ValueError("Immutable staged file already has different content")
        finally:
            os.close(parent_fd)
    os.fsync(attempt_dir)


def stage_input(config, request):
    """Resume a manifest-bound input under the same lock used by submission."""
    manifest = request["manifest"]
    require_management_protocol(manifest)
    attempt_id = uuid_value(request["attempt_id"])
    path = request["path"]
    entries = [entry for entry in manifest["files"] if entry["path"] == path]
    if len(entries) != 1:
        raise ValueError("Undeclared staged input")
    entry = entries[0]
    if not 0 <= entry["size_bytes"] <= 8 * 1024**3 or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
        raise ValueError("Invalid input descriptor")
    parts = components(path)
    if len(path) > 240:
        raise ValueError("Invalid staged input path")
    root = open_directory(config["remote_root"], create=True)
    try:
        journal = open_directory("_broker_journal", create=True, parent=root)
        try:
            lock = open_regular(journal, attempt_id + ".lock", os.O_RDWR | os.O_CREAT)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                record = load_json(journal, attempt_id + ".json")
                if record:
                    require_journal_binding(record, digest(canonical(manifest)))
                    raise ValueError("Attempt already has submission intent; reconcile it")
                relative = "/".join((uuid_value(manifest["request"]["project_id"]), uuid_value(manifest["job_revision_id"]), attempt_id))
                directory = open_directory(relative, create=True, parent=root)
                try:
                    parent = open_directory("/".join(parts[:-1]), create=True, parent=directory) if len(parts) > 1 else os.dup(directory)
                    try:
                        try:
                            final = open_regular(parent, parts[-1])
                        except FileNotFoundError:
                            final = None
                        if final is not None:
                            try:
                                if os.fstat(final).st_size != entry["size_bytes"] or file_digest(final) != entry["sha256"]:
                                    raise ValueError("Staged input changed")
                            finally:
                                os.close(final)
                            return {"offset": entry["size_bytes"], "complete": True}
                        temporary = "_relay-part-" + entry["sha256"]
                        fd = open_regular(parent, temporary, os.O_RDWR | os.O_CREAT)
                        try:
                            size = os.fstat(fd).st_size
                            if "content_base64" not in request:
                                return {"offset": size, "complete": False}
                            offset = request["offset"]
                            data = base64.b64decode(request["content_base64"], validate=True)
                            if type(offset) is not int or not 0 <= offset <= size or len(data) > 1024 * 1024 or offset + len(data) > entry["size_bytes"]:
                                raise ValueError("Invalid input transfer range")
                            if offset < size:
                                if os.pread(fd, len(data), offset) != data:
                                    raise ValueError("Input retry changed bytes")
                            else:
                                with os.fdopen(os.dup(fd), "r+b") as stream:
                                    stream.seek(offset)
                                    stream.write(data)
                                    stream.flush()
                                    os.fsync(stream.fileno())
                            size = os.fstat(fd).st_size
                            complete = request.get("finish", False)
                            if type(complete) is not bool:
                                raise ValueError("Invalid finish flag")
                            if complete:
                                if size != entry["size_bytes"] or file_digest(fd) != entry["sha256"]:
                                    raise ValueError("Input checksum mismatch")
                                os.fchmod(fd, 0o500 if entry["executable"] else 0o400)
                                os.fsync(fd)
                                os.rename(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                                os.fsync(parent)
                            return {"offset": size, "complete": complete}
                        finally:
                            os.close(fd)
                    finally:
                        os.close(parent)
                finally:
                    os.close(directory)
            finally:
                os.close(lock)
        finally:
            os.close(journal)
    finally:
        os.close(root)


def own_record(config, identity, expected_manifest_sha256):
    attempt_id = uuid_value(identity["attempt_id"])
    root = open_directory(config["remote_root"])
    try:
        journal = open_directory("_broker_journal", parent=root)
        try:
            record = load_json(journal, attempt_id + ".json")
        finally:
            os.close(journal)
        if not record or record.get("outcome") != "submitted" or record["native_identity"] != identity:
            raise ValueError("Native identity is not owned by this broker attempt")
        require_journal_binding(record, expected_manifest_sha256)
        manifest = record["manifest"]
        relative = "/".join((uuid_value(manifest["request"]["project_id"]),
                             uuid_value(manifest["job_revision_id"]), attempt_id))
        if (record.get("attempt_path") != relative
                or identity.get("work_dir") != config["remote_root"].rstrip("/") + "/" + relative):
            raise ValueError("Native directory identity differs from approved package")
        return record
    finally:
        os.close(root)


def submission_time_matches(value, created_at):
    try:
        submitted = datetime.datetime.fromisoformat(value)
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=datetime.timezone.utc)
        created = datetime.datetime.fromisoformat(created_at)
        return created - datetime.timedelta(seconds=60) <= submitted <= created + datetime.timedelta(seconds=120)
    except (ValueError, TypeError):
        return False


def submit(config, request):
    attempt_id = uuid_value(request["attempt_id"])
    manifest = request["manifest"]
    require_management_protocol(manifest)
    project = uuid_value(manifest["request"]["project_id"])
    revision = uuid_value(manifest["job_revision_id"])
    manifest_digest = digest(canonical(manifest))
    root = open_directory(config["remote_root"], create=True)
    journal = open_directory("_broker_journal", create=True, parent=root)
    lock = open_regular(journal, attempt_id + ".lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = load_json(journal, attempt_id + ".json")
        if record:
            require_journal_binding(record, manifest_digest)
            return record
        attempt_path = "/".join((project, revision, attempt_id))
        attempt_dir = open_directory(attempt_path, create=True, parent=root)
        try:
            stage(attempt_dir, manifest, request["files"])
        finally:
            os.close(attempt_dir)
        record = {"outcome": "submission_uncertain", "manifest_sha256": manifest_digest,
                  "attempt_path": attempt_path, "manifest": manifest,
                  "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "detail": "Durable submit intent recorded; reconcile before any retry"}
        write_json(journal, attempt_id + ".json", record)
        absolute_attempt = config["remote_root"].rstrip("/") + "/" + attempt_path
        try:
            result = scheduler(config, "sbatch", ["--parsable", "--comment=broker-attempt:" + attempt_id,
                                                manifest["batch_script"]], cwd=absolute_attempt)
        except Exception:
            return record
        if result["returncode"]:
            # An RPC/connection error can be reported after the controller has
            # accepted the job. Nonzero sbatch alone never proves non-submission.
            record["detail"] = "Scheduler returned nonzero; acceptance remains uncertain"
            record["diagnostic"] = result["stderr"][:2048]
            write_json(journal, attempt_id + ".json", record)
            return record
        match = re.fullmatch(r"([0-9]+)(?:;([A-Za-z0-9_.-]+))?\s*", result["stdout"])
        if not match:
            return record
        identity = {"attempt_id": attempt_id, "job_id": match.group(1), "cluster": match.group(2) or "",
                    "submitted_at": record["created_at"], "work_dir": absolute_attempt}
        record.update(outcome="submitted", native_identity=identity, detail="Accepted by Slurm")
        write_json(journal, attempt_id + ".json", record)
        return record
    finally:
        os.close(lock)
        os.close(journal)
        os.close(root)


def reconcile(config, request):
    attempt_id = uuid_value(request["attempt_id"])
    require_management_protocol(request["manifest"])
    expected = digest(canonical(request["manifest"]))
    if request.get("manifest_sha256") != expected:
        raise ValueError("Reconciliation prepared manifest digest mismatch")
    root = open_directory(config["remote_root"], create=True)
    journal = open_directory("_broker_journal", create=True, parent=root)
    lock = open_regular(journal, attempt_id + ".lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = load_json(journal, attempt_id + ".json")
        if not record:
            return {"outcome": "submission_uncertain", "detail": "No durable remote receipt; non-submission cannot be established"}
        require_journal_binding(record, expected)
        if record["outcome"] != "submission_uncertain":
            return record
        # Restrict recovery by remote owner, name, marker, and a finite time window.
        # Accounting without stored comments cannot resolve this ambiguity.
        user = pwd.getpwuid(os.getuid()).pw_name
        try:
            response = scheduler(config, "squeue", ["-h", "--user=" + user,
                                                   "--name=" + request["manifest"]["request"]["name"], "-o", "%i|%k|%V"])
        except Exception:
            response = {"returncode": -1, "stdout": ""}
        matches = set()
        if response["returncode"] == 0:
            for line in response["stdout"].splitlines():
                fields = line.split("|")
                if len(fields) == 3 and fields[0].isdigit() and fields[1] == "broker-attempt:" + attempt_id and submission_time_matches(fields[2], record["created_at"]):
                    matches.add(fields[0])
        if not matches:
            created = datetime.datetime.fromisoformat(record["created_at"])
            start = created - datetime.timedelta(seconds=60)
            now = datetime.datetime.now(datetime.timezone.utc)
            # This is a bounded recovery query, not an unrestricted history scan.
            end = min(now + datetime.timedelta(seconds=60), created + datetime.timedelta(days=30))
            try:
                accounting = scheduler(config, "sacct", ["-X", "-D", "-P", "-n", "--user=" + user,
                    "--name=" + request["manifest"]["request"]["name"],
                    "--starttime=" + start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "--endtime=" + end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "--format=JobIDRaw,Comment,Submit"])
            except Exception:
                accounting = {"returncode": -1, "stdout": ""}
            if accounting["returncode"] == 0:
                for line in accounting["stdout"].splitlines():
                    fields = line.split("|")
                    if len(fields) >= 3 and fields[0].isdigit() and fields[1] == "broker-attempt:" + attempt_id:
                        try:
                            submitted = datetime.datetime.fromisoformat(fields[2]).replace(tzinfo=datetime.timezone.utc)
                        except ValueError:
                            continue
                        if start <= submitted <= end and submission_time_matches(fields[2], record["created_at"]):
                            matches.add(fields[0])
        if len(matches) == 1:
            record.update(outcome="submitted", native_identity={"attempt_id": attempt_id,
                          "job_id": next(iter(matches)), "cluster": "", "submitted_at": record["created_at"],
                          "work_dir": config["remote_root"].rstrip("/") + "/" + record["attempt_path"]},
                          detail="Recovered native identity from broker attempt marker")
            write_json(journal, attempt_id + ".json", record)
        return record
    finally:
        os.close(lock)
        os.close(journal)
        os.close(root)
