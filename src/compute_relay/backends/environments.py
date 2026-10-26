"""Trusted existing-environment profiles; rendering never executes discovery or jobs.

References: Python venv (activation is optional), Conda run, and Modules/Lmod
initialization. Existing installations remain mutable references, not image digests.
"""

from __future__ import annotations

import posixpath
import re
import shlex

from compute_relay.remote.environment_shell import (
    MODULE_INIT_PATHS, EnvironmentProfileError, absolute_environment_path, module_init_path, module_shell,
)


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
    if kind == "system" and (modules or environment.get("module_init") or "module_paths" in environment):
        raise EnvironmentProfileError("Choose a module or virtual environment profile for module initialization")
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
    lines.extend(module_shell(environment))
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
