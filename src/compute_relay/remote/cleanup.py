"""Bounded removal of one journal-bound terminal attempt; journals stay durable."""

import datetime
import fcntl
import os
import stat

from .filesystem import open_directory, open_regular, uuid_value, write_json
from .jobs import own_record, submission_time_matches
from .status import observe
from .protocol import JOB_PROTOCOL
from .scheduler import scheduler_rows


def cleanup_terminal(config, request, record):
    identity = request["native_identity"]
    result = observe(config, {"operation": "observe", "protocol": JOB_PROTOCOL, "items": [
        {"native_identity": identity, "manifest_sha256": request["manifest_sha256"]}]})["items"][identity["attempt_id"]]
    queue, accounting = result["queue"], result["accounting"]
    if queue["returncode"] or queue["stdout"].strip() or accounting["returncode"]:
        raise ValueError("Remote cleanup requires terminal scheduler evidence")
    rows = scheduler_rows(accounting, 11)
    if len(rows) != 1 or len(rows[0]) != 11:
        raise ValueError("Remote cleanup requires one exact native allocation")
    row = [field.strip() for field in rows[0]]
    terminal = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "REVOKED", "PREEMPTED"}
    marker = "broker-attempt:" + identity["attempt_id"]
    native_state = row[1].split("+", 1)[0].split()[0] if row[1] else "UNKNOWN"
    if (row[0] != identity["job_id"] or native_state not in terminal
            or not submission_time_matches(row[6], record["created_at"])
            or row[9] and row[9] != marker
            or not (row[9] == marker or row[10] == identity["work_dir"])):
        raise ValueError("Remote cleanup allocation identity differs")
    datetime.datetime.fromisoformat(row[8])


def cleanup_tree(directory, *, delete=False, depth=0, budget=None):
    if budget is None:
        budget = {"entries": 0, "bytes": 0, "device": os.fstat(directory).st_dev}
    if depth > 32:
        raise ValueError("Remote cleanup depth exceeds its limit")
    with os.scandir(directory) as entries:
        for entry in entries:
            budget["entries"] += 1
            if budget["entries"] > 10000:
                raise ValueError("Remote cleanup entry count exceeds its limit")
            info = entry.stat(follow_symlinks=False)
            if info.st_dev != budget["device"]:
                raise ValueError("Remote cleanup cannot cross filesystems")
            if stat.S_ISDIR(info.st_mode):
                child = open_directory(entry.name, parent=directory)
                try:
                    cleanup_tree(child, delete=delete, depth=depth + 1, budget=budget)
                finally:
                    os.close(child)
                if delete:
                    os.rmdir(entry.name, dir_fd=directory)
            elif stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                budget["bytes"] += info.st_size
                if budget["bytes"] > 2**63 - 1:
                    raise ValueError("Remote cleanup size exceeds its receipt limit")
                if delete:
                    os.unlink(entry.name, dir_fd=directory)
            else:
                raise ValueError("Remote cleanup encountered an unsupported filesystem entry")
    if delete:
        os.fsync(directory)
    return budget


def cleanup(config, request):
    identity = request["native_identity"]
    attempt = uuid_value(identity["attempt_id"])
    root = open_directory(config["remote_root"])
    try:
        journal = open_directory("_broker_journal", parent=root)
        try:
            lock = open_regular(journal, attempt + ".lock", os.O_RDWR | os.O_CREAT)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                record = own_record(config, identity, request["manifest_sha256"])
                if record["manifest"].get("extension_name"):
                    raise ValueError("Managed extension directories cannot be cleaned")
                saved = record.get("cleanup", {})
                if saved.get("state") == "complete":
                    return saved
                cleanup_terminal(config, request, record)
                relative = record["attempt_path"].split("/")
                parent = open_directory("/".join(relative[:-1]), parent=root)
                try:
                    try:
                        directory = open_directory(relative[-1], parent=parent)
                    except FileNotFoundError:
                        if saved.get("state") != "deleting":
                            raise ValueError("Missing attempt has no durable cleanup intent") from None
                    else:
                        try:
                            measured = cleanup_tree(directory)
                            if saved.get("state") != "deleting":
                                saved = {"state": "deleting", "size_bytes": measured["bytes"], "entries": measured["entries"],
                                    "retained_journal": True}
                            record["cleanup"] = saved
                            write_json(journal, attempt + ".json", record)
                            cleanup_tree(directory, delete=True)
                        finally:
                            os.close(directory)
                        os.rmdir(relative[-1], dir_fd=parent)
                        os.fsync(parent)
                    saved = {**saved, "state": "complete"}
                    record["cleanup"] = saved
                    write_json(journal, attempt + ".json", record)
                    return saved
                finally:
                    os.close(parent)
            finally:
                os.close(lock)
        finally:
            os.close(journal)
    finally:
        os.close(root)
