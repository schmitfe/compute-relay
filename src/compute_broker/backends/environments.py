"""Trusted existing-environment profiles; rendering never executes discovery or jobs.

References: Python venv (activation is optional), Conda run, and Modules/Lmod
initialization. Existing installations remain mutable references, not image digests.
"""

from __future__ import annotations

import posixpath
import re
import shlex


MODULE_INIT_PATHS = (
    "/etc/profile.d/modules.sh", "/etc/profile.d/lmod.sh", "/etc/profile.d/z00_lmod.sh",
    "/usr/share/Modules/init/bash", "/usr/share/modules/init/bash", "/usr/share/lmod/lmod/init/bash",
)


class EnvironmentProfileError(ValueError):
    pass


def absolute_environment_path(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value == "/" or len(value) > 4096:
        raise EnvironmentProfileError("Environment paths must be dedicated absolute paths")
    if posixpath.normpath(value) != value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise EnvironmentProfileError("Environment paths must be canonical without control characters")
    return value


def module_init_path(value: object) -> str:
    path = absolute_environment_path(value)
    if path not in MODULE_INIT_PATHS and not re.fullmatch(r"/usr/share/lmod/[0-9][A-Za-z0-9_.-]*/init/bash", path):
        raise EnvironmentProfileError("Module initializer must be a known system initialization path")
    return path


def render_environment(environment: dict, logical_interpreter: str) -> tuple[str, list[str]]:
    allowed = {"id", "revision_sha256", "kind", "interpreter", "modules", "module_init", "module_paths", "prefix", "conda_executable"}
    if not isinstance(environment, dict) or set(environment) - allowed:
        raise EnvironmentProfileError("Unrecognized existing-environment configuration field")
    modules = environment.get("modules", [])
    if not isinstance(modules, list) or len(modules) > 32:
        raise EnvironmentProfileError("Modules must be a list of at most 32 names")
    for name in modules:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./+@:-]{0,159}", name) or any(p in (".", "..", "") for p in name.split("/")):
            raise EnvironmentProfileError("Invalid module name")
    kind = environment.get("kind", "modules" if modules else "system")
    if kind not in {"system", "modules", "venv", "conda", "poetry"}:
        raise EnvironmentProfileError("Unsupported existing-environment kind")
    if kind != "modules" and (modules or environment.get("module_init") or "module_paths" in environment):
        raise EnvironmentProfileError("Module initialization requires a modules profile")
    if kind in {"system", "modules"} and environment.get("prefix"):
        raise EnvironmentProfileError("An environment prefix requires a virtual environment profile")
    if logical_interpreter not in {"python3", "bash"}:
        raise EnvironmentProfileError("Unsupported logical interpreter")
    prefix = None
    if kind in {"venv", "conda", "poetry"}:
        if logical_interpreter != "python3":
            raise EnvironmentProfileError("Existing virtual environments currently require a Python job")
        if environment.get("prefix"):
            prefix = absolute_environment_path(environment["prefix"])
    default_interpreter = logical_interpreter if kind == "modules" else f"/usr/bin/{logical_interpreter}"
    if prefix:
        default_interpreter = prefix + "/bin/python"
    executable = environment.get("interpreter", default_interpreter)
    if not isinstance(executable, str):
        raise EnvironmentProfileError("Environment interpreter must be a path or module-provided name")
    if executable.startswith("/"):
        absolute_environment_path(executable)
    elif kind != "modules" or "/" in executable:
        raise EnvironmentProfileError("Only module profiles may use a bare interpreter name")
    basename = posixpath.basename(executable)
    if logical_interpreter == "python3":
        if not re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", basename):
            raise EnvironmentProfileError("A Python profile requires a Python/Python3 interpreter")
    elif basename != "bash":
        raise EnvironmentProfileError("A Bash profile requires a Bash interpreter")
    lines = ["#!/bin/bash", "# Approved existing environment; no installation occurs on worker or login node.",
             "# Referenced modules, binaries and packages may change outside this broker."]
    if kind == "modules":
        initializer = environment.get("module_init", "")
        if initializer:
            initializer = module_init_path(initializer)
        elif not isinstance(initializer, str):
            raise EnvironmentProfileError("Module initialization must be an optional path")
        module_path_export = None
        if "module_paths" in environment:
            paths = environment["module_paths"]
            if not isinstance(paths, list) or not 1 <= len(paths) <= 16:
                raise EnvironmentProfileError("A module profile needs between 1 and 16 explicit module paths")
            paths = [absolute_environment_path(path) for path in paths]
            if any(":" in path for path in paths):
                raise EnvironmentProfileError("A module path cannot contain a path-list separator")
            module_path_export = "export MODULEPATH=" + shlex.quote(":".join(paths))
            lines.append(module_path_export)
        if initializer:
            lines += ["if ! command -v module >/dev/null 2>&1; then",
                      "  if [ ! -r " + shlex.quote(initializer) + " ]; then",
                      "    printf '%s\\n' " + shlex.quote("BROKER_ENVIRONMENT_ERROR: MODULE_INITIALIZER_UNREADABLE: " + initializer) + " >&2",
                      "    exit 127", "  fi", "  . " + shlex.quote(initializer), "fi"]
        lines += ["if ! command -v module >/dev/null 2>&1; then",
                  "  printf '%s\\n' 'BROKER_ENVIRONMENT_ERROR: MODULE_COMMAND_UNAVAILABLE: The cluster login shell did not provide the module command.' >&2",
                  "  exit 127", "fi"]
        lines.append("module purge")
        if module_path_export:
            # Initializers and unloading default modules may rewrite MODULEPATH.
            # Restore the approved list immediately before loading the selection.
            lines.append(module_path_export)
        if modules:
            lines.append("module load " + shlex.join(modules))
    command = [executable]
    if kind in {"venv", "poetry", "conda"}:
        if prefix is None:
            if posixpath.basename(posixpath.dirname(executable)) != "bin":
                raise EnvironmentProfileError("An existing environment interpreter must be beneath its bin directory")
            prefix = posixpath.dirname(posixpath.dirname(executable))
            absolute_environment_path(prefix)
        if posixpath.dirname(executable) != prefix + "/bin":
            raise EnvironmentProfileError("Environment interpreter must belong to the selected prefix")
        if kind in {"venv", "poetry"}:
            # Activation is unnecessary for the interpreter itself. Set PATH for
            # subprocesses without sourcing user activation/project hooks.
            lines += ["export VIRTUAL_ENV=" + shlex.quote(prefix),
                      "export PATH=" + shlex.quote(prefix + "/bin") + ':"${PATH:-/usr/bin:/bin}"']
            if kind == "poetry":
                lines.append("# Uses an existing Poetry-managed venv directly; Poetry/project hooks are not invoked.")
        else:
            conda = absolute_environment_path(environment.get("conda_executable"))
            if posixpath.basename(conda) != "conda":
                raise EnvironmentProfileError("Conda executable must be the explicitly approved conda binary")
            lines += ["export CONDA_NO_PLUGINS=true", "# Conda activation hooks, if present, run inside the authorized compute job."]
            command = [conda, "run", "--no-capture-output", "--prefix", prefix, executable]
    if kind != "conda" and environment.get("conda_executable"):
        raise EnvironmentProfileError("A Conda executable requires a conda profile")
    return "\n".join(lines) + "\n", command


def validate_environment_config(environment: dict) -> None:
    executable = environment.get("interpreter", "python3") if isinstance(environment, dict) else "python3"
    logical = "bash" if isinstance(executable, str) and posixpath.basename(executable) == "bash" else "python3"
    render_environment(environment, logical)
