"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import datetime
import json
import re
import subprocess

from .prelude import NAME


def scheduler_rows(response, fields):
    """Validate framing before filtering: mutable fields cannot create records."""
    rows = []
    for line in response.get("stdout", "").splitlines():
        if not line.strip():
            continue
        row = [field.strip() for field in line.split("|")]
        if len(row) == fields + 1 and row[-1] == "":
            row.pop()
        if len(row) != fields:
            raise ValueError("Malformed scheduler record; identity and accounting remain uncertain")
        rows.append(row)
    return rows


def _sacct_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate sacct JSON field")
        result[key] = value
    return result


def _sacct_constant(value):
    raise ValueError("Nonfinite sacct JSON number")


def _sacct_number(value):
    if isinstance(value, dict):
        if value.get("set") is not True or value.get("infinite") is not False:
            raise ValueError("Unset sacct number")
        value = value["number"]
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError("Invalid sacct number")
    return value


def _sacct_text(value):
    # The compact protocol is framed only after JSON has separated free text.
    if (not isinstance(value, str) or len(value) > 4096 or value != value.strip()
            or any(char in value for char in "|\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029\x00")
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError("Unrepresentable sacct text")
    return value


def _sacct_timestamp(value, *, required=False):
    number = _sacct_number(value)
    if number == 0:
        if required:
            raise ValueError("Missing sacct submission time")
        return ""
    return datetime.datetime.fromtimestamp(number, datetime.timezone.utc).isoformat()


def _sacct_row(job):
    identifier = job["job_id"]
    if type(identifier) is not int or not 1 <= identifier < 10**20:
        raise ValueError("Invalid sacct allocation ID")
    state = job["state"]["current"]
    if isinstance(state, list) and len(state) == 1:
        state = state[0]
    if not isinstance(state, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", state):
        raise ValueError("Ambiguous sacct state")
    times = job["time"]
    elapsed = _sacct_number(times["elapsed"])
    allocated = job["tres"]["allocated"]
    if not isinstance(allocated, list) or len(allocated) > 256:
        raise ValueError("Missing sacct allocation")
    resources = {}
    for entry in allocated:
        kind, name = entry["type"], entry["name"]
        if not isinstance(kind, str) or name is not None and not isinstance(name, str):
            raise ValueError("Invalid sacct resource descriptor")
        if kind == "cpu":
            if name is not None:
                raise ValueError("Ambiguous sacct CPU resource")
            key = "cpu"
        elif kind == "gres" and isinstance(name, str) and (name == "gpu" or name.startswith("gpu:")):
            if not re.fullmatch(r"gpu(?::[A-Za-z0-9_.-]+)?", name):
                raise ValueError("Invalid sacct GPU type")
            key = "gres/" + name
        else:
            continue
        if key in resources:
            raise ValueError("Duplicate sacct allocation resource")
        resources[key] = _sacct_number(entry["count"])
    if "cpu" not in resources:
        # An explicit empty allocation at zero elapsed is distinct from missing
        # allocation data, e.g. cancellation before any resources were granted.
        if allocated or elapsed:
            raise ValueError("Missing sacct CPU allocation")
        resources["cpu"] = 0
    exit_code = job["exit_code"]
    status = exit_code["status"]
    if status not in {"SUCCESS", "ERROR", "SIGNALED"}:
        raise ValueError("Unknown sacct exit status")
    code = exit_code["return_code"]
    signal = exit_code.get("signal")
    if status == "SIGNALED":
        signal = _sacct_number(signal["signal_id"])
        if signal == 0:
            raise ValueError("Missing sacct termination signal")
        code = 0 if code is None else _sacct_number(code)
    else:
        code = _sacct_number(code)
        signal = 0 if signal is None else _sacct_number(signal["signal_id"])
    comment = job["comment"]["job"]
    comment = _sacct_text("" if comment is None else comment)
    workdir = _sacct_text(job["working_directory"])
    return [str(identifier), state, str(elapsed), str(resources["cpu"]),
        ",".join(key + "=" + str(value) for key, value in resources.items()), str(code) + ":" + str(signal),
        _sacct_timestamp(times["submission"], required=True), _sacct_timestamp(times["start"]),
        _sacct_timestamp(times["end"]), comment, workdir]


def sacct_response(response):
    """Normalize bounded structured accounting; never parse free-text records."""
    try:
        if response["returncode"] != 0:
            raise ValueError("sacct failed")
        source = response["stdout"]
        if not isinstance(source, str) or len(source.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("sacct JSON exceeds its limit")
        data = json.loads(source, object_pairs_hook=_sacct_pairs, parse_constant=_sacct_constant)
        jobs = data["jobs"]
        if data["errors"] != [] or not isinstance(jobs, list) or len(jobs) > 1024:
            raise ValueError("Invalid sacct result")
        rows = [_sacct_row(job) for job in jobs]
        return {"returncode": 0, "stdout": "\n".join("|".join(row) for row in rows), "stderr": ""}
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, RecursionError, OSError):
        return {"returncode": 1, "stdout": "", "stderr": "Invalid structured accounting; reconciliation deferred"}


def scheduler(config, command, args, *, cwd=None, timeout=30):
    path = config.get("slurm_paths", {}).get(command, "/usr/bin/" + command)
    if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", path):
        raise ValueError("Scheduler executable must be an approved absolute path")
    env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "TZ": "UTC"}
    # Configured cluster-specific values are admin-controlled, never agent data.
    extra = config.get("scheduler_environment", {})
    if not isinstance(extra, dict) or any(not NAME.fullmatch(k) or k.startswith("SBATCH_") or not isinstance(v, str) for k, v in extra.items()):
        raise ValueError("Invalid scheduler environment")
    env.update(extra)
    result = subprocess.run([path, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, cwd=cwd, env=env, check=False)
    if len(result.stdout) > 2 * 1024 * 1024 or len(result.stderr) > 65536:
        raise ValueError("Scheduler output exceeds helper limits")
    return {"returncode": result.returncode, "stdout": result.stdout.decode("utf-8", "replace"),
            "stderr": result.stderr.decode("utf-8", "replace")}
