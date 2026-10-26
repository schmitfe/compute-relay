"""Owner-requested login-node inspection; no installation or project execution."""

import json
import os
import pwd
import shutil
import stat

from .environment_shell import absolute_environment_path, inspection_request, module_shell
from .probes import software_command


def executable_metadata(path):
    try:
        absolute_environment_path(path)
        info = os.stat(path)
        return stat.S_ISREG(info.st_mode) and os.access(path, os.X_OK)
    except (OSError, ValueError, TypeError):
        return False


def inspect_environment(config, request):
    selection = inspection_request(request["environment"])
    home = pwd.getpwuid(os.getuid()).pw_dir
    # This explicit human action uses the site's login shell, like its batch
    # wrappers. No discovered interpreter or Poetry/project command is executed.
    script = "set -e\n" + "\n".join(module_shell(selection))
    script += "\nprintf '\\0RELAY_ENV\\0'\n/usr/bin/env -0\n"
    probe = software_command(["/bin/bash", "-l", "-c", script], extra_env={"HOME": home}, timeout=12)
    if probe["returncode"] or probe["truncated"] or "\x00RELAY_ENV\x00" not in probe["stdout"]:
        return {"ok": False, "error_code": "ENVIRONMENT_MODULE_LOAD_FAILED"}
    observed = dict(entry.split("=", 1) for entry in probe["stdout"].split("\x00RELAY_ENV\x00", 1)[1].split("\x00") if "=" in entry)
    path = observed.get("PATH", "/usr/bin:/bin")
    python = []
    for name in ["python3", "python", *["python3." + str(version) for version in range(8, 16)]]:
        candidate = shutil.which(name, path=path)
        if candidate and executable_metadata(candidate) and candidate not in python:
            python.append(candidate)
    if selection.get("interpreter") and executable_metadata(selection["interpreter"]) and selection["interpreter"] not in python:
        python.append(selection["interpreter"])
    conda = selection.get("conda_executable") or shutil.which("conda", path=path) or observed.get("CONDA_EXE")
    poetry = shutil.which("poetry", path=path)
    result = {"ok": True, "scope": "login_node", "verified_on_compute": False,
              "python": python, "conda_executable": conda if executable_metadata(conda) else "",
              "poetry_executable": poetry if executable_metadata(poetry) else "",
              "environments": [], "conda_status": "not_found", "truncated": False}
    prefixes = []
    if result["conda_executable"]:
        # Preserve selected module runtime libraries; isolate config/plugins and
        # HOME so Conda cannot rewrite the user's environment registry/config.
        extra = {key: observed[key] for key in ("PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH") if key in observed}
        result["conda_status"] = "unavailable"
        try:
            query = software_command([conda, "info", "--envs", "--json"], extra_env=extra, timeout=10)
            data = json.loads(query["stdout"])
            if query["returncode"] or query["truncated"] or not isinstance(data, dict) or not isinstance(data.get("envs"), list):
                raise ValueError("Conda inspection failed")
            prefixes = data["envs"][:128]
            result["truncated"] = len(data["envs"]) > 128
            result["conda_status"] = "available"
        except (OSError, ValueError, TypeError):
            pass
        # Read bounded prefix metadata rather than giving Conda the real HOME.
        try:
            descriptor = os.open(os.path.join(home, ".conda", "environments.txt"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("Conda registry is not a regular file")
                raw = os.read(descriptor, 65537)
            finally:
                os.close(descriptor)
            result["truncated"] |= len(raw) > 65536
            prefixes.extend(raw[:65536].decode("utf-8", "replace").splitlines()[:128])
        except (OSError, ValueError):
            pass
    candidates = [("conda", prefix) for prefix in prefixes]
    candidates += [("venv", observed.get("VIRTUAL_ENV")), ("conda", observed.get("CONDA_PREFIX"))]
    if selection.get("prefix"):
        candidates.append(("venv", selection["prefix"]))
    try:
        with os.scandir(os.path.join(home, ".cache", "pypoetry", "virtualenvs")) as entries:
            for index, entry in enumerate(entries):
                if index >= 128:
                    result["truncated"] = True
                    break
                if entry.is_dir(follow_symlinks=False):
                    candidates.append(("poetry", entry.path))
    except OSError:
        pass
    seen = set()
    for kind, prefix in candidates:
        try:
            prefix = absolute_environment_path(prefix)
        except ValueError:
            continue
        interpreter = prefix + "/bin/python"
        if prefix in seen or not executable_metadata(interpreter):
            continue
        seen.add(prefix)
        if os.path.isdir(prefix + "/conda-meta"):
            kind = "conda"
        result["environments"].append({"kind": kind, "prefix": prefix, "interpreter": interpreter})
        if len(result["environments"]) >= 128 or len(json.dumps(result)) > 96000:
            if len(json.dumps(result)) > 96000:
                result["environments"].pop()
            result["truncated"] = True
            break
    return result
