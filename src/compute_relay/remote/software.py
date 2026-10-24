"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import json
import os
import re
import stat
import subprocess
import time

from .discovery_config import (
    MODULE_INITIALIZERS, available_modules, discovery_config, discovery_path,
    interpreter_candidate, trusted_system_path,
)
from .filesystem import open_directory, read_file
from .module_catalog import (
    augment_module_availability, automatic_module_inventory, effective_module_paths,
    metadata_module_inventory,
)
from .probes import software_command


def software_discovery(config):
    """Login-node observations only; no installations or compute-node probes.

    Module --terse avail follows Modules/Lmod APIs; Conda uses env list --json.
    Poetry candidates are existing venv metadata beneath explicitly configured
    roots, never `poetry run` or project configuration/plugin execution.
    """
    snapshot = {"scope": "login_node", "verified_on_compute": False,
                "modules": {"status": "not_found", "available": [], "module_paths": [], "truncated": False},
                "conda": {"status": "not_configured", "prefixes": [], "truncated": False},
                "python": [], "environments": [],
                "search": {"visited_directories": 0, "truncated": False, "roots": []},
                "notes": ["Candidates were observed on the login node; compute-node availability is unverified.",
                          "Existing environment contents can change independently of the approved wrapper.",
                          "Poetry ownership is inferred from the configured root, not verified by running Poetry.",
                          "An empty module inventory may require explicit trusted site module_paths; personal/login startup is not read."]}
    try:
        options = discovery_config(config)
        if options["module_inventory_mode"] == "availability":
            module_paths, skipped_module_paths = effective_module_paths(options)
    except ValueError:
        snapshot["status"] = "invalid_configuration"
        return snapshot
    if options["module_inventory_mode"] == "auto":
        snapshot["modules"] = automatic_module_inventory(options)
        if options["module_availability_enabled"]:
            augment_module_availability(snapshot["modules"])
    elif options["module_inventory_mode"] == "metadata":
        snapshot["modules"] = metadata_module_inventory(options)
    else:
        snapshot["modules"].update(module_paths=module_paths, skipped_paths=skipped_module_paths,
            path_source="configured" if "module_paths" in options else "automatic", inventory_mode="availability",
            source="module_terse_avail", provisional=False)
    snapshot["status"] = "available"
    for path in options["python_executables"]:
        try:
            info = os.stat(path)
            if stat.S_ISREG(info.st_mode) and info.st_mode & 0o111:
                snapshot["python"].append({"kind": "system", "interpreter": path, "source": "configured_path_metadata", "verified_on_compute": False})
        except OSError:
            pass
    initializers = [] if options["module_inventory_mode"] in {"metadata", "auto"} else ([options["module_init"]] if options.get("module_init") else MODULE_INITIALIZERS)
    for initializer in initializers:
        try:
            # Only root-owned, non-writable system initialization files may run.
            trusted_system_path(initializer, directory=False)
            snapshot["modules"]["init_path"] = initializer
            if not module_paths:
                snapshot["modules"]["status"] = "configuration_required"
                break
            import shlex
            probe = software_command(["/bin/bash", "--noprofile", "--norc", "-c",
                ". " + shlex.quote(initializer) + " >/dev/null 2>&1; export MODULEPATH=" + shlex.quote(":".join(module_paths)) + "; module --terse avail"],
                extra_env={"MODULEPATH": ":".join(module_paths), "LMOD_TERSE_DECORATIONS": "no"})
            names = available_modules(probe["stdout"] + "\n" + probe["stderr"])
            snapshot["modules"].update(status="available" if probe["returncode"] == 0 else "unavailable",
                                       available=names[:512], truncated=probe["truncated"] or len(names) > 512)
            break
        except (OSError, ValueError, subprocess.SubprocessError):
            snapshot["modules"]["status"] = "unavailable"
    conda = options.get("conda_executable")
    if conda:
        snapshot["conda"]["executable"] = conda
        try:
            conda_roots = [root["path"] for root in options["search_roots"] if root["kind"] == "conda"]
            probe = software_command([conda, "env", "list", "--json"], extra_env={"CONDA_ENVS_PATH": os.pathsep.join(conda_roots) or "/nonexistent"})
            data = json.loads(probe["stdout"])
            prefixes = data.get("envs", []) if isinstance(data, dict) else []
            if probe["returncode"] or not isinstance(prefixes, list):
                raise ValueError("Conda query failed")
            valid = []
            for prefix in prefixes[:128]:
                try:
                    valid.append(discovery_path(prefix))
                except ValueError:
                    continue
            snapshot["conda"].update(status="available", prefixes=valid, truncated=probe["truncated"] or len(prefixes) > 128)
            for prefix in valid:
                executable = interpreter_candidate(prefix)
                if executable:
                    snapshot["environments"].append({"kind": "conda", "prefix": prefix, "interpreter": executable,
                        "source": "conda_env_list", "verified_on_compute": False})
        except (OSError, ValueError, subprocess.SubprocessError):
            snapshot["conda"]["status"] = "unavailable"
    deadline = time.monotonic() + 4
    total_entries = 0
    for root in options["search_roots"]:
        observation = {**root, "status": "available"}
        snapshot["search"]["roots"].append(observation)
        pending = [(root["path"], 0)]
        while pending:
            if snapshot["search"]["visited_directories"] >= 256 or total_entries >= 4096 or time.monotonic() >= deadline:
                snapshot["search"]["truncated"] = True
                observation["status"] = "truncated"
                break
            path, depth = pending.pop()
            try:
                directory = open_directory(path)
            except OSError:
                if depth == 0:
                    observation["status"] = "unavailable"
                continue
            try:
                snapshot["search"]["visited_directories"] += 1
                metadata = {}
                try:
                    text = read_file(directory, "pyvenv.cfg", 16384).decode("utf-8", "replace")
                    metadata = {key.strip(): val.strip() for line in text.splitlines() for key, sep, val in [line.partition("=")] if sep}
                except (OSError, ValueError):
                    pass
                conda_directory = False
                try:
                    conda_fd = open_directory("conda-meta", parent=directory)
                    conda_directory = True
                    os.close(conda_fd)
                except OSError:
                    pass
                kind = root["kind"]
                if metadata or conda_directory or kind == "python":
                    executable = interpreter_candidate(path)
                    if executable:
                        candidate = {"kind": "conda" if conda_directory else ("poetry" if kind == "poetry" else "venv" if metadata else "python"),
                                     "prefix": path, "interpreter": executable, "source": "configured_root_metadata", "verified_on_compute": False}
                        if re.fullmatch(r"[0-9]+(?:\.[0-9A-Za-z+-]+){1,3}", metadata.get("version", "")):
                            candidate["python_version"] = metadata["version"]
                        snapshot["environments"].append(candidate)
                        # Candidate internals (site-packages etc.) are never scanned.
                        continue
                if depth < root["max_depth"]:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            total_entries += 1
                            if total_entries >= 4096:
                                snapshot["search"]["truncated"] = True
                                break
                            if entry.name.startswith(".") and entry.name != ".venv":
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                pending.append((path + "/" + entry.name, depth + 1))
            except OSError:
                observation["status"] = "unavailable"
            finally:
                os.close(directory)
    unique = {(item["kind"], item["interpreter"]): item for item in snapshot["environments"]}
    snapshot["environments"] = list(unique.values())[:256]
    if len(unique) > 256:
        snapshot["search"]["truncated"] = True
    return snapshot
