"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import base64

def collect(config, request, identity, record):
    outputs = request.get("outputs", [])
    if set(outputs) - set(record["manifest"]["request"]["outputs"]):
        raise ValueError("Undeclared output requested")
    root = open_directory(config["remote_root"])
    directory = open_directory(record["attempt_path"], parent=root)
    try:
        result = {"stdout": "", "stderr": "", "artifacts_base64": {}}
        for stream in ("stdout", "stderr"):
            try:
                result[stream] = read_file(directory, stream + ".log", 256 * 1024, tail=True).decode("utf-8", "replace")
            except FileNotFoundError:
                pass
        total = 0
        for path in outputs:
            try:
                content = read_file(directory, path, 10 * 1024 * 1024 - total)
            except FileNotFoundError:
                continue
            total += len(content)
            result["artifacts_base64"][path] = base64.b64encode(content).decode("ascii")
        return result
    finally:
        os.close(directory)
        os.close(root)
