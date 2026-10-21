"""Small-surface one-shot helper: fixed operations, never imported job code.

This file is sent verbatim to a protected absolute Python interpreter through SSH.
It deliberately uses only the standard library and starts no persistent service.
Remote same-UID/admin tampering remains outside its integrity guarantee.
"""

import base64
import datetime
import fcntl
import hashlib
import json
import os
import pwd
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid


MAX_REQUEST = 150 * 1024 * 1024
MAX_FILE_TOTAL = 104857600
NOFOLLOW = os.O_NOFOLLOW
DIRECTORY = os.O_DIRECTORY
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
MODULE_INITIALIZERS = (
    "/etc/profile.d/modules.sh", "/etc/profile.d/lmod.sh", "/etc/profile.d/z00_lmod.sh",
    "/usr/share/Modules/init/bash", "/usr/share/modules/init/bash", "/usr/share/lmod/lmod/init/bash",
)
SYSTEM_MODULE_PATHS = (
    "/etc/modulefiles", "/usr/share/modulefiles", "/usr/share/Modules/modulefiles",
    "/usr/share/lmod/lmod/modulefiles", "/opt/modulefiles", "/usr/local/modulefiles",
)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def uuid_value(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Noncanonical attempt identity")
    return value


def components(path):
    if not isinstance(path, str) or not path or len(path) > 4096 or any(c in path for c in "\x00\r\n"):
        raise ValueError("Invalid path")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("Invalid path component")
    return parts


def open_directory(path, *, create=False, parent=None):
    if parent is None:
        if not isinstance(path, str) or not path.startswith("/") or path == "/":
            raise ValueError("A dedicated absolute remote root is required")
        fd = os.open("/", os.O_RDONLY | DIRECTORY)
        parts = components(path[1:])
    else:
        fd = os.dup(parent)
        parts = components(path)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_regular(directory, path, flags=os.O_RDONLY, mode=0o600):
    parts = components(path)
    fd = os.dup(directory)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        result = os.open(parts[-1], flags | NOFOLLOW, mode, dir_fd=fd)
        st = os.fstat(result)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            os.close(result)
            raise ValueError("Only regular files without additional hard links are supported")
        return result
    finally:
        os.close(fd)


def read_file(directory, path, cap, tail=False):
    fd = open_regular(directory, path)
    try:
        before = os.fstat(fd)
        if not tail and before.st_size > cap:
            raise ValueError("File exceeds artifact byte limit")
        if tail:
            os.lseek(fd, max(0, before.st_size - cap), os.SEEK_SET)
        chunks = []
        left = cap
        while left:
            chunk = os.read(fd, min(65536, left))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        after = os.fstat(fd)
        if not tail and (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Artifact changed during retrieval")
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_json(directory, name, data):
    temp = ".journal-" + uuid.uuid4().hex
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600, dir_fd=directory)
    try:
        payload = canonical(data)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temp, dir_fd=directory)
        except FileNotFoundError:
            pass


def load_json(directory, name):
    try:
        return json.loads(read_file(directory, name, 2 * 1024 * 1024))
    except FileNotFoundError:
        return None


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


def software_command(argv, *, extra_env=None, timeout=8):
    """Bound both live output and time for fixed, operator-enabled probes.

    HOME/config/plugin isolation avoids personal shell startup and Conda registry
    writes. This never invokes a discovered Python or a project/Poetry command.
    """
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
           "PYTHONNOUSERSITE": "1", "CONDA_NO_PLUGINS": "true", "CONDARC": "/dev/null"}
    env.update(extra_env or {})
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd="/", env=env, start_new_session=True)
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    truncated = False
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                truncated = True
                break
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = streams[key.fileobj]
                available = 65536 - len(target)
                target.extend(chunk[:available])
                if len(target) >= 65536:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return {"returncode": process.returncode, "stdout": bytes(streams[process.stdout]).decode("utf-8", "replace"),
            "stderr": bytes(streams[process.stderr]).decode("utf-8", "replace"), "truncated": truncated}


def discovery_path(value):
    if not isinstance(value, str) or not value.startswith("/") or value == "/" or len(value) > 4096 or os.path.normpath(value) != value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Discovery requires an explicit canonical absolute path")
    return value


def discovery_config(config):
    value = config.get("environment_discovery", {})
    if not isinstance(value, dict) or set(value) - {"module_init", "module_paths", "module_inventory_mode", "conda_executable", "python_executables", "search_roots"}:
        raise ValueError("Invalid software discovery configuration")
    result = dict(value)
    mode = value.get("module_inventory_mode", "availability")
    if not isinstance(mode, str) or mode not in {"availability", "metadata"}:
        raise ValueError("Unsupported module inventory mode")
    result["module_inventory_mode"] = mode
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


def trusted_module_tree(path, budget):
    """Bounded metadata audit before module availability can evaluate rc files."""
    trusted_system_path(path, directory=True)
    pending = [path]
    while pending:
        if budget["entries"] >= 4096 or budget["directories"] >= 256 or time.monotonic() >= budget["deadline"]:
            raise ValueError("Module path metadata audit limit reached")
        current = pending.pop()
        trusted_system_path(current, directory=True)
        budget["directories"] += 1
        with os.scandir(current) as entries:
            for entry in entries:
                budget["entries"] += 1
                if budget["entries"] >= 4096 or time.monotonic() >= budget["deadline"]:
                    raise ValueError("Module path metadata audit limit reached")
                info = entry.stat(follow_symlinks=False)
                if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
                    raise ValueError("Module tree contains user-writable or non-system entries")
                full_path = current + "/" + entry.name
                if stat.S_ISLNK(info.st_mode):
                    # Do not recursively follow directory aliases (cycles/escapes).
                    # An operator can configure the resolved trusted directory.
                    if entry.is_dir(follow_symlinks=True):
                        raise ValueError("Module tree contains a directory symlink")
                    trusted_system_path(full_path, directory=False)
                elif stat.S_ISDIR(info.st_mode):
                    pending.append(full_path)
                elif not stat.S_ISREG(info.st_mode):
                    raise ValueError("Module tree contains a special file")


def effective_module_paths(options):
    configured = "module_paths" in options
    candidates = options["module_paths"] if configured else SYSTEM_MODULE_PATHS
    paths, skipped = [], []
    budget = {"entries": 0, "directories": 0, "deadline": time.monotonic() + 2}
    for path in dict.fromkeys(candidates):
        try:
            trusted_module_tree(path, budget)
            paths.append(path)
        except (OSError, ValueError) as exc:
            if configured:
                raise ValueError("Explicit module path failed trust or bounded metadata checks") from exc
            skipped.append({"path": path, "status": "not_found" if isinstance(exc, FileNotFoundError) else "not_trusted_or_audit_limited"})
    return paths, skipped


def metadata_module_directory_permissions(info, path, *, final, ancestor_warnings):
    if not info.st_mode & 0o022:
        return
    # A provisional name catalog may cross these two narrowly defined shared
    # system ancestors. This never makes module code trusted for evaluation.
    permitted_ancestor = (not final and info.st_uid == 0 and
                          (not info.st_mode & 0o002 or info.st_mode & stat.S_ISVTX))
    if not permitted_ancestor:
        raise ValueError("Module path ancestor/root allows unsupported group/world writes")
    warning = {"path": path, "uid": info.st_uid, "mode": oct(stat.S_IMODE(info.st_mode)),
               "reason": "root_owned_sticky_ancestor" if info.st_mode & 0o002 else "root_owned_group_writable_ancestor"}
    if warning not in ancestor_warnings:
        if len(ancestor_warnings) >= 64:
            raise ValueError("Module metadata ancestor warning bound reached")
        ancestor_warnings.append(warning)


def open_metadata_module_root(path, *, deadline=None, ancestor_warnings=None):
    """Open an explicit resolved directory, checking every ancestor without links.

    Unlike availability discovery, names alone need no root-owner guarantee.
    The configured tree excludes group/world writes. Narrow root-owned shared
    ancestors are permitted with warnings for this provisional metadata only.
    No file content, shell initialization or module code is read/evaluated here.
    """
    path = discovery_path(path)
    home = pwd.getpwuid(os.getuid()).pw_dir.rstrip("/")
    if path in {home, "/home", "/users", "/root", "/usr", "/usr/bin", "/usr/local", "/opt", "/var", "/tmp", "/etc", "/projects"}:
        raise ValueError("Module inventory needs a dedicated configured directory")
    if os.path.realpath(path) != path:
        raise ValueError("Metadata module paths must be explicit resolved paths")
    ancestor_warnings = ancestor_warnings if ancestor_warnings is not None else []
    fd = os.open("/", os.O_RDONLY | DIRECTORY | NOFOLLOW)
    try:
        metadata_module_directory_permissions(os.fstat(fd), "/", final=False, ancestor_warnings=ancestor_warnings)
        parts = components(path[1:])
        current = ""
        for index, part in enumerate(parts):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Module metadata deadline reached during ancestor checks")
            next_fd = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            current += "/" + part
            metadata_module_directory_permissions(os.fstat(fd), current, final=index == len(parts) - 1,
                                                  ancestor_warnings=ancestor_warnings)
        return fd
    except BaseException:
        os.close(fd)
        raise


def metadata_module_name(relative):
    parts = relative.split("/")
    if any(part.startswith(".") for part in parts):
        return None
    basename = parts[-1]
    stem = basename[:-4] if basename.endswith((".lua", ".tcl")) else basename
    housekeeping = {"default", "latest", "modulerc", "alias", "aliases", "version", "readme", "license",
                    "licence", "copying", "changelog", "install", "authors", "news", "makefile", "pkgindex",
                    "initrc", "modulepath", "modulespath"}
    if stem.lower() in housekeeping:
        return None
    # Numeric dots in extensionless version names (3.12.1, 2024.1rc2) remain
    # valid; unrelated scripts/docs/binaries with alphabetic suffixes do not.
    if stem == basename and re.search(r"\.[A-Za-z][A-Za-z0-9]*$", basename):
        return None
    name = "/".join([*parts[:-1], stem])
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./+@:-]{0,159}", name) or any(part in {"", ".", ".."} for part in name.split("/")):
        return None
    return name


def metadata_module_inventory(options):
    """A provisional filename catalog; never a module-availability assertion."""
    result = {"status": "unavailable", "available": [], "module_paths": [], "path_source": "configured",
              "inventory_mode": "metadata", "source": "configured_module_file_metadata", "provisional": True,
              "explanation": "Provisional names from regular module-file paths only; module code, aliases, defaults and dependencies were not evaluated. Availability and execution compatibility are unverified.",
              "truncated": False, "partial": False, "warnings": [], "roots": [], "skipped_paths": [],
              "ancestor_warnings": [],
              "visited_entries": 0, "visited_directories": 0,
              "skipped": {"symlinks": 0, "unsafe_permissions": 0, "housekeeping_or_names": 0,
                          "special_files": 0, "depth_limit": 0, "inaccessible": 0}}
    if options.get("module_init"):
        try:
            # This checks a known system file and its metadata only. Conventional
            # root-owned versioned initializer links are allowed; never source it.
            trusted_system_path(options["module_init"], directory=False)
            result["init_path"] = options["module_init"]
            result["initializer_status"] = "metadata_verified"
        except (OSError, ValueError):
            result["initializer_status"] = "unsafe_or_unavailable"
            result["partial"] = True
            result["warnings"].append("Configured module initializer did not pass metadata checks; it was not evaluated.")
    deadline = time.monotonic() + 2
    names = set()
    for root in dict.fromkeys(options["module_paths"]):
        observation = {"path": root, "status": "unavailable"}
        result["roots"].append(observation)
        if result["visited_entries"] >= 4096 or result["visited_directories"] >= 256 or len(names) >= 512 or time.monotonic() >= deadline:
            result["truncated"] = result["partial"] = True
            observation["status"] = "not_scanned_limit"
            result["skipped_paths"].append({"path": root, "status": "not_scanned_limit"})
            continue
        try:
            root_fd = open_metadata_module_root(root, deadline=deadline, ancestor_warnings=result["ancestor_warnings"])
        except (OSError, ValueError) as exc:
            result["partial"] = True
            reason = "not_found" if isinstance(exc, FileNotFoundError) else "not_scanned_limit" if isinstance(exc, TimeoutError) else "unsafe_or_inaccessible"
            if isinstance(exc, TimeoutError):
                result["truncated"] = True
            observation["status"] = reason
            result["skipped_paths"].append({"path": root, "status": reason})
            continue
        observation["status"] = "available"
        result["module_paths"].append(root)
        pending = [(root_fd, "", 0)]
        try:
            while pending:
                if result["visited_entries"] >= 4096 or result["visited_directories"] >= 256 or len(names) >= 512 or time.monotonic() >= deadline:
                    result["truncated"] = result["partial"] = True
                    observation["status"] = "truncated"
                    break
                directory, prefix, depth = pending.pop()
                try:
                    # Check the opened inode again, not just the earlier entry.
                    if os.fstat(directory).st_mode & 0o022:
                        result["skipped"]["unsafe_permissions"] += 1
                        result["partial"] = True
                        continue
                    result["visited_directories"] += 1
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if result["visited_entries"] >= 4096 or len(names) >= 512 or time.monotonic() >= deadline:
                                result["truncated"] = result["partial"] = True
                                observation["status"] = "truncated"
                                break
                            result["visited_entries"] += 1
                            if entry.name.startswith(".") or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+@:-]{0,159}", entry.name):
                                result["skipped"]["housekeeping_or_names"] += 1
                                continue
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISLNK(info.st_mode):
                                result["skipped"]["symlinks"] += 1
                                result["partial"] = True
                                continue
                            if info.st_mode & 0o022:
                                result["skipped"]["unsafe_permissions"] += 1
                                result["partial"] = True
                                continue
                            relative = prefix + "/" + entry.name if prefix else entry.name
                            if stat.S_ISDIR(info.st_mode):
                                if depth >= 8:
                                    result["skipped"]["depth_limit"] += 1
                                    result["truncated"] = result["partial"] = True
                                    continue
                                # Pending handles are bounded too; do not open
                                # thousands of siblings before visiting any.
                                if result["visited_directories"] + len(pending) >= 256:
                                    result["truncated"] = result["partial"] = True
                                    observation["status"] = "truncated"
                                    continue
                                child = os.open(entry.name, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=directory)
                                pending.append((child, relative, depth + 1))
                            elif stat.S_ISREG(info.st_mode):
                                name = metadata_module_name(relative)
                                if name:
                                    names.add(name)
                                else:
                                    result["skipped"]["housekeeping_or_names"] += 1
                            else:
                                result["skipped"]["special_files"] += 1
                                result["partial"] = True
                except OSError:
                    result["skipped"]["inaccessible"] += 1
                    result["partial"] = True
                    observation["status"] = "partial"
                finally:
                    os.close(directory)
        finally:
            for directory, _, _ in pending:
                os.close(directory)
    result["available"] = sorted(names)
    if result["module_paths"]:
        result["status"] = "available" if names else "empty"
    if result["partial"]:
        result["warnings"].append("Catalog is partial: unsafe/inaccessible paths, symbolic links or traversal limits were omitted; inspect roots and skipped counters.")
    if result["truncated"]:
        result["warnings"].append("Metadata scan reached its time, depth, entry, directory or catalog-name bound.")
    if result["ancestor_warnings"]:
        result["warnings"].append("Configured roots cross root-owned writable shared ancestors. This provisional metadata exception grants no authority to evaluate module code; inspect ancestor_warnings.")
    return result


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
    if options["module_inventory_mode"] == "metadata":
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
    initializers = [] if options["module_inventory_mode"] == "metadata" else ([options["module_init"]] if options.get("module_init") else MODULE_INITIALIZERS)
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


def stage(attempt_dir, manifest, encoded_files):
    entries = manifest["files"]
    if len(entries) > 110 or set(encoded_files) != {entry["path"] for entry in entries}:
        raise ValueError("Package file set mismatch")
    total = 0
    for entry in entries:
        path = entry["path"]
        parts = components(path)
        if path.startswith("/") or len(path) > 240:
            raise ValueError("Invalid staged file path")
        data = base64.b64decode(encoded_files[path], validate=True)
        total += len(data)
        if total > MAX_FILE_TOTAL or len(data) != entry["size_bytes"] or digest(data) != entry["sha256"]:
            raise ValueError("Staged file differs from approved manifest")
        parent_fd = open_directory("/".join(parts[:-1]), create=True, parent=attempt_dir) if len(parts) > 1 else os.dup(attempt_dir)
        try:
            try:
                existing = read_file(parent_fd, parts[-1], MAX_FILE_TOTAL)
            except FileNotFoundError:
                temporary = "_upload_" + uuid.uuid4().hex
                mode = 0o500 if entry["executable"] else 0o400
                fd = open_regular(parent_fd, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Atomic rename occurs inside the per-attempt broker lock.
                    # Same-UID malicious mutation remains outside this boundary.
                    os.rename(temporary, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    os.fsync(parent_fd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
            else:
                if digest(existing) != entry["sha256"]:
                    raise ValueError("Immutable staged file already has different content")
        finally:
            os.close(parent_fd)
    os.fsync(attempt_dir)


def own_record(config, identity):
    attempt_id = uuid_value(identity["attempt_id"])
    root = open_directory(config["remote_root"])
    try:
        journal = open_directory("_broker_journal", parent=root)
        try:
            record = load_json(journal, attempt_id + ".json")
        finally:
            os.close(journal)
        if not record or record.get("outcome") != "submitted" or record["native_identity"] != identity:
            raise ValueError("Native identity is not owned by this broker attempt")
        return record
    finally:
        os.close(root)


def submission_time_matches(value, created_at):
    try:
        submitted = datetime.datetime.fromisoformat(value)
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=datetime.timezone.utc)
        created = datetime.datetime.fromisoformat(created_at)
        return created - datetime.timedelta(seconds=60) <= submitted <= created + datetime.timedelta(seconds=120)
    except (ValueError, TypeError):
        return False


def submit(config, request):
    attempt_id = uuid_value(request["attempt_id"])
    manifest = request["manifest"]
    project = uuid_value(manifest["request"]["project_id"])
    revision = uuid_value(manifest["job_revision_id"])
    manifest_digest = digest(canonical(manifest))
    root = open_directory(config["remote_root"], create=True)
    journal = open_directory("_broker_journal", create=True, parent=root)
    lock = open_regular(journal, attempt_id + ".lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = load_json(journal, attempt_id + ".json")
        if record:
            if record["manifest_sha256"] != manifest_digest:
                raise ValueError("Attempt identity is already bound to different content")
            return record
        attempt_path = "/".join((project, revision, attempt_id))
        attempt_dir = open_directory(attempt_path, create=True, parent=root)
        try:
            stage(attempt_dir, manifest, request["files"])
        finally:
            os.close(attempt_dir)
        record = {"outcome": "submission_uncertain", "manifest_sha256": manifest_digest,
                  "attempt_path": attempt_path, "manifest": manifest,
                  "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "detail": "Durable submit intent recorded; reconcile before any retry"}
        write_json(journal, attempt_id + ".json", record)
        absolute_attempt = config["remote_root"].rstrip("/") + "/" + attempt_path
        try:
            result = scheduler(config, "sbatch", ["--parsable", "--comment=broker-attempt:" + attempt_id,
                                                manifest["batch_script"]], cwd=absolute_attempt)
        except Exception:
            return record
        if result["returncode"]:
            # An RPC/connection error can be reported after the controller has
            # accepted the job. Nonzero sbatch alone never proves non-submission.
            record["detail"] = "Scheduler returned nonzero; acceptance remains uncertain"
            record["diagnostic"] = result["stderr"][:2048]
            write_json(journal, attempt_id + ".json", record)
            return record
        match = re.fullmatch(r"([0-9]+)(?:;([A-Za-z0-9_.-]+))?\s*", result["stdout"])
        if not match:
            return record
        identity = {"attempt_id": attempt_id, "job_id": match.group(1), "cluster": match.group(2) or "",
                    "submitted_at": record["created_at"], "work_dir": absolute_attempt}
        record.update(outcome="submitted", native_identity=identity, detail="Accepted by Slurm")
        write_json(journal, attempt_id + ".json", record)
        return record
    finally:
        os.close(lock)
        os.close(journal)
        os.close(root)


def reconcile(config, request):
    attempt_id = uuid_value(request["attempt_id"])
    root = open_directory(config["remote_root"], create=True)
    journal = open_directory("_broker_journal", create=True, parent=root)
    lock = open_regular(journal, attempt_id + ".lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = load_json(journal, attempt_id + ".json")
        if not record:
            return {"outcome": "submission_uncertain", "detail": "No durable remote receipt; non-submission cannot be established"}
        if record["manifest_sha256"] != digest(canonical(request["manifest"])):
            raise ValueError("Reconciliation manifest mismatch")
        if record["outcome"] != "submission_uncertain":
            return record
        # Restrict recovery by remote owner, name, marker, and a finite time window.
        # Accounting without stored comments cannot resolve this ambiguity.
        user = pwd.getpwuid(os.getuid()).pw_name
        try:
            response = scheduler(config, "squeue", ["-h", "--user=" + user,
                                                   "--name=" + request["manifest"]["request"]["name"], "-o", "%i|%k|%V"])
        except Exception:
            response = {"returncode": -1, "stdout": ""}
        matches = set()
        if response["returncode"] == 0:
            for line in response["stdout"].splitlines():
                fields = line.split("|")
                if len(fields) == 3 and fields[0].isdigit() and fields[1] == "broker-attempt:" + attempt_id and submission_time_matches(fields[2], record["created_at"]):
                    matches.add(fields[0])
        if not matches:
            created = datetime.datetime.fromisoformat(record["created_at"])
            start = created - datetime.timedelta(seconds=60)
            now = datetime.datetime.now(datetime.timezone.utc)
            # This is a bounded recovery query, not an unrestricted history scan.
            end = min(now + datetime.timedelta(seconds=60), created + datetime.timedelta(days=30))
            try:
                accounting = scheduler(config, "sacct", ["-X", "-D", "-P", "-n", "--user=" + user,
                    "--name=" + request["manifest"]["request"]["name"],
                    "--starttime=" + start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "--endtime=" + end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "--format=JobIDRaw,Comment,Submit"])
            except Exception:
                accounting = {"returncode": -1, "stdout": ""}
            if accounting["returncode"] == 0:
                for line in accounting["stdout"].splitlines():
                    fields = line.split("|")
                    if len(fields) >= 3 and fields[0].isdigit() and fields[1] == "broker-attempt:" + attempt_id:
                        try:
                            submitted = datetime.datetime.fromisoformat(fields[2]).replace(tzinfo=datetime.timezone.utc)
                        except ValueError:
                            continue
                        if start <= submitted <= end and submission_time_matches(fields[2], record["created_at"]):
                            matches.add(fields[0])
        if len(matches) == 1:
            record.update(outcome="submitted", native_identity={"attempt_id": attempt_id,
                          "job_id": next(iter(matches)), "cluster": "", "submitted_at": record["created_at"],
                          "work_dir": config["remote_root"].rstrip("/") + "/" + record["attempt_path"]},
                          detail="Recovered native identity from broker attempt marker")
            write_json(journal, attempt_id + ".json", record)
        return record
    finally:
        os.close(lock)
        os.close(journal)
        os.close(root)


def execute(config, request):
    op = request.get("operation")
    if op == "submit":
        return submit(config, request)
    if op == "reconcile":
        return reconcile(config, request)
    if op == "discover":
        def query(command, args):
            try:
                return scheduler(config, command, args)
            except Exception as exc:
                return {"returncode": -1, "stdout": "", "stderr": type(exc).__name__}

        settings = query("scontrol", ["show", "config"])
        # Return only relevant scheduler facts, not the entire server configuration.
        setting_names = {"KillWait", "OverTimeLimit", "JobRequeue", "PrologFlags", "SelectType", "SelectTypeParameters", "PreemptMode", "AccountingStoreFlags"}
        settings["stdout"] = "\n".join(line for line in settings["stdout"].splitlines()
                                         if line.split("=", 1)[0].strip() in setting_names)
        user = pwd.getpwuid(os.getuid()).pw_name
        return {"version": query("sinfo", ["--version"]),
                "nodes": query("sinfo", ["-N", "-h", "-o", "%N|%P|%c|%m|%G|%t"]),
                "partitions": query("scontrol", ["show", "partition", "-o"]),
                "settings": settings,
                "software": software_discovery(config),
                "associations": query("sacctmgr", ["-n", "-P", "show", "association", "where", "user=" + user,
                                                       "format=User,Account,Partition,QOS,DefaultQOS"])}
    identity = request["native_identity"]
    if not re.fullmatch(r"[0-9]{1,20}", identity.get("job_id", "")):
        raise ValueError("Invalid native job ID")
    record = own_record(config, identity)
    job_id = identity["job_id"]
    if op == "observe":
        return {"queue": scheduler(config, "squeue", ["-h", "-j", job_id, "-o", "%i|%T|%k|%V"]),
                "accounting": scheduler(config, "sacct", ["-j", job_id, "-X", "-D", "-P", "-n",
                    "--format=JobIDRaw,State,ElapsedRaw,AllocCPUS,AllocTRES,ExitCode,Submit,Start,End,Comment%128,WorkDir%4096"]),
                "expected_gpus": record["manifest"]["maximum_allocation"]["gpu_devices"]}
    if op == "cancel":
        live = scheduler(config, "squeue", ["-h", "-j", job_id, "-o", "%i|%k|%V"])
        rows = [line.split("|") for line in live["stdout"].splitlines() if line.strip()]
        marker = "broker-attempt:" + identity["attempt_id"]
        if live["returncode"] or len(rows) != 1 or len(rows[0]) != 3 or rows[0][0] != job_id or rows[0][1] != marker or not submission_time_matches(rows[0][2], record["created_at"]):
            return {"outcome": "unknown", "detail": "Live job identity did not match; cancellation refused"}
        user = pwd.getpwuid(os.getuid()).pw_name
        result = scheduler(config, "scancel", ["--user=" + user, "--name=" + record["manifest"]["request"]["name"], "--", job_id])
        return {"outcome": "cancel_requested" if result["returncode"] == 0 else "unknown"}
    if op == "collect":
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
    raise ValueError("Unsupported fixed helper operation")


def main():
    os.umask(0o077)
    payload = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(payload) > MAX_REQUEST:
        raise ValueError("Remote request exceeds limit")
    request = json.loads(payload)
    response = execute(request["config"], request)
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Nonzero exit is intentionally ambiguous to the caller for submit.
        sys.stderr.write("Broker remote operation failed: " + type(exc).__name__ + "\n")
        sys.exit(1)
