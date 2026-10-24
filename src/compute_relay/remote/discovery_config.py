"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import os
import pwd
import re
import stat

MODULE_INITIALIZERS = (
    "/etc/profile.d/modules.sh", "/etc/profile.d/lmod.sh", "/etc/profile.d/z00_lmod.sh",
    "/usr/share/Modules/init/bash", "/usr/share/modules/init/bash", "/usr/share/lmod/lmod/init/bash",
)

SYSTEM_MODULE_PATHS = (
    "/etc/modulefiles", "/usr/share/modulefiles", "/usr/share/Modules/modulefiles",
    "/usr/share/lmod/lmod/modulefiles", "/opt/modulefiles", "/usr/local/modulefiles",
)

MODULE_PATH_CONFIGURATION_FILES = (
    "/etc/profile", "/etc/environment", "/etc/lmod/.modulespath", "/etc/lmod/modulespath",
    "/etc/environment-modules/modulespath", "/etc/environment-modules/initrc",
    "/usr/share/Modules/init/.modulespath", "/usr/share/modules/init/.modulespath",
)

MODULE_PROFILE_DIRECTORIES = ("/etc/profile.d",)


def discovery_path(value):
    if not isinstance(value, str) or not value.startswith("/") or value == "/" or len(value) > 4096 or os.path.normpath(value) != value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Discovery requires an explicit canonical absolute path")
    return value


def discovery_config(config):
    value = config.get("environment_discovery", {})
    if not isinstance(value, dict) or set(value) - {"module_init", "module_paths", "module_inventory_mode", "module_availability_enabled", "conda_executable", "python_executables", "search_roots"}:
        raise ValueError("Invalid software discovery configuration")
    result = dict(value)
    mode = value.get("module_inventory_mode", "availability")
    if not isinstance(mode, str) or mode not in {"availability", "metadata", "auto"}:
        raise ValueError("Unsupported module inventory mode")
    result["module_inventory_mode"] = mode
    availability = value.get("module_availability_enabled", False)
    if type(availability) is not bool or (availability and mode != "auto"):
        raise ValueError("Optional module availability requires automatic mode and a boolean opt-in")
    result["module_availability_enabled"] = availability
    if value.get("module_init") is not None:
        initializer = discovery_path(value["module_init"])
        if initializer not in MODULE_INITIALIZERS and not re.fullmatch(r"/usr/share/lmod/[0-9][A-Za-z0-9_.-]*/init/bash", initializer):
            raise ValueError("Module initializer is not a known system path")
    if "module_paths" in value:
        paths = value["module_paths"]
        if not isinstance(paths, list) or len(paths) > 16:
            raise ValueError("At most 16 explicit module paths are supported")
        result["module_paths"] = [discovery_path(path) for path in paths]
        if any(":" in path for path in result["module_paths"]):
            raise ValueError("A module path cannot contain a path-list separator")
    if mode == "metadata" and not result.get("module_paths"):
        raise ValueError("Metadata module inventory requires explicit nonempty module paths")
    if value.get("conda_executable") is not None:
        if os.path.basename(discovery_path(value["conda_executable"])) != "conda":
            raise ValueError("Expected an explicitly configured conda executable")
    pythons = value.get("python_executables", ["/usr/bin/python3", "/usr/local/bin/python3"])
    if not isinstance(pythons, list) or len(pythons) > 16:
        raise ValueError("Too many configured Python paths")
    result["python_executables"] = [discovery_path(path) for path in pythons]
    if any(not re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", os.path.basename(path)) for path in result["python_executables"]):
        raise ValueError("Configured Python paths must name Python interpreters")
    roots = value.get("search_roots", [])
    if not isinstance(roots, list) or len(roots) > 8:
        raise ValueError("Too many environment search roots")
    home = pwd.getpwuid(os.getuid()).pw_dir.rstrip("/")
    result["search_roots"] = []
    for root in roots:
        if not isinstance(root, dict) or set(root) - {"path", "kind", "max_depth"} or "path" not in root:
            raise ValueError("Invalid environment search root")
        path = discovery_path(root["path"])
        if path in {home, "/home", "/users", "/root", "/usr", "/usr/bin", "/usr/local", "/opt", "/var", "/tmp", "/etc"}:
            raise ValueError("Environment search root must be a dedicated environment directory")
        if root.get("kind", "venv") not in {"venv", "conda", "poetry", "python"} or type(root.get("max_depth", 2)) is not int or not 0 <= root.get("max_depth", 2) <= 3:
            raise ValueError("Unsupported environment search kind/depth")
        result["search_roots"].append({"path": path, "kind": root.get("kind", "venv"), "max_depth": root.get("max_depth", 2)})
    return result


def interpreter_candidate(prefix):
    # Following an interpreter symlink is metadata inspection only; venv commonly
    # links Python to its base installation. Directory traversal remains no-follow.
    for basename in ("python", "python3"):
        path = prefix + "/bin/" + basename
        try:
            info = os.stat(path)
            if stat.S_ISREG(info.st_mode) and info.st_mode & 0o111:
                return path
        except OSError:
            pass
    return None


def available_modules(text):
    names = set()
    # Terse APIs emit one name per line, directory headings end in ':', and
    # Lmod may decorate hidden/default names. Never treat prose as a module.
    for line in text.splitlines():
        value = re.sub(r"(?:\([^)]*\)|<[A-Za-z]{1,3}>)$", "", line.strip()).strip()
        if value.endswith(("/", ":")) or value.startswith("/") or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./+@:-]{0,159}", value):
            continue
        if all(part not in {"", ".", ".."} for part in value.split("/")):
            names.add(value)
    return sorted(names)


def trusted_system_path(path, *, directory):
    """Check root ownership and write protection of paths and all ancestors.

    Root-owned symlinks are allowed for conventional versioned Lmod installs;
    their resolved targets and target ancestors must satisfy the same checks.
    This is a metadata snapshot, not a defense against remote administrators.
    """
    path = discovery_path(path)
    for candidate in {path, os.path.realpath(path)}:
        current = "/"
        pieces = candidate.lstrip("/").split("/")
        for index, piece in enumerate(["", *pieces]):
            if piece:
                current = os.path.join(current, piece)
            info = os.lstat(current)
            if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
                raise ValueError("Module paths must be root-owned and non-writable by other users")
            if index < len(pieces) and not (stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise ValueError("Invalid module path ancestor")
        info = os.stat(candidate)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise ValueError("Unexpected module path type")
