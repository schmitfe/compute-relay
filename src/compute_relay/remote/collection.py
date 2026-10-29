"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import base64
import os

from .filesystem import open_directory, open_regular, file_digest, file_version


def output_transfer(config, request, record):
    root = open_directory(config["remote_root"])
    try:
        directory = open_directory(record["attempt_path"], parent=root)
        try:
            if request["operation"] == "output_manifest":
                entries, missing, total = [], [], 0
                for path in record["manifest"]["request"]["outputs"]:
                    try:
                        fd = open_regular(directory, path)
                    except FileNotFoundError:
                        missing.append(path)
                        continue
                    try:
                        info = os.fstat(fd)
                        total += info.st_size
                        if info.st_size > 8 * 1024**3 or total > 64 * 1024**3:
                            raise ValueError("Outputs exceed transfer limits")
                        entries.append({"path": path, "size_bytes": info.st_size,
                            "sha256": file_digest(fd), "version": file_version(info)})
                    finally:
                        os.close(fd)
                return {"files": entries, "missing": missing}
            path, offset = request["path"], request["offset"]
            if path not in record["manifest"]["request"]["outputs"] or type(offset) is not int or offset < 0:
                raise ValueError("Undeclared output or invalid offset")
            fd = open_regular(directory, path)
            try:
                before = os.fstat(fd)
                if file_version(before) != request["version"] or offset > before.st_size:
                    raise ValueError("Output changed since transfer started")
                content = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
                if file_version(os.fstat(fd)) != request["version"]:
                    raise ValueError("Output changed during transfer")
                return {"offset": offset, "content_base64": base64.b64encode(content).decode("ascii")}
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    finally:
        os.close(root)


def read_logs(config, request, record):
    root = open_directory(config["remote_root"])
    try:
        directory = open_directory(record["attempt_path"], parent=root)
        try:
            result = {}
            for stream in ("stdout", "stderr"):
                state = request.get("cursors", {}).get(stream, {})
                cursor = state.get("cursor", 0)
                if type(cursor) is not int or not 0 <= cursor <= 2**53 - 1:
                    raise ValueError("Invalid log cursor")
                try:
                    fd = open_regular(directory, stream + ".log")
                except FileNotFoundError:
                    continue
                try:
                    info = os.fstat(fd)
                    identity = str(info.st_dev) + ":" + str(info.st_ino)
                    reset = state.get("file_id") != identity or cursor > info.st_size
                    if reset:
                        cursor = 0
                    # Bound lag as well as memory. Explicitly report any gap.
                    start = max(cursor, info.st_size - 65536)
                    content = os.pread(fd, min(65536, info.st_size - start), start)
                    result[stream] = {"cursor": start, "next_cursor": start + len(content),
                        "size_bytes": info.st_size, "file_id": identity, "reset": reset,
                        "truncated": start > cursor, "content_base64": base64.b64encode(content).decode("ascii")}
                finally:
                    os.close(fd)
            return result
        finally:
            os.close(directory)
    finally:
        os.close(root)
