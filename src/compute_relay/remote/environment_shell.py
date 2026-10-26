"""Fixed module shell setup shared by reviewed jobs and owner-only probes."""

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


def module_shell(environment):
    modules = environment.get("modules", [])
    if not isinstance(modules, list) or len(modules) > 32:
        raise EnvironmentProfileError("Modules must be a list of at most 32 names")
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./+@:-]{0,159}", name)
           or any(part in {"", ".", ".."} for part in name.split("/")) for name in modules):
        raise EnvironmentProfileError("Invalid module name")
    lines = []
    if environment.get("kind") == "modules" or modules or environment.get("module_init") or environment.get("module_paths"):
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
    return lines


def inspection_request(value):
    if not isinstance(value, dict) or set(value) - {"modules", "module_init", "module_paths", "conda_executable", "prefix", "interpreter"}:
        raise ValueError("Invalid environment inspection")
    module_shell({**value, "kind": "modules"})
    for key in ("conda_executable", "prefix", "interpreter"):
        if key in value and not isinstance(value[key], str):
            raise ValueError("Expected an optional environment path")
        if value.get(key):
            absolute_environment_path(value[key])
    if value.get("conda_executable") and posixpath.basename(value["conda_executable"]) != "conda":
        raise ValueError("Expected a conda executable")
    if value.get("interpreter") and not re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", posixpath.basename(value["interpreter"])):
        raise ValueError("Expected a Python interpreter")
    return value


