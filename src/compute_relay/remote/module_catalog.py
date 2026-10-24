"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import os
import pwd
import re
import stat
import subprocess
import time

from .discovery_config import (
    MODULE_INITIALIZERS, MODULE_PATH_CONFIGURATION_FILES,
    MODULE_PROFILE_DIRECTORIES, SYSTEM_MODULE_PATHS, available_modules,
    discovery_path, trusted_system_path,
)
from .filesystem import components
from .prelude import DIRECTORY, NOFOLLOW
from .probes import software_command

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


def read_trusted_module_configuration(path, maximum):
    """Read only bounded root-owned system configuration; never source it."""
    trusted_system_path(path, directory=False)
    resolved = os.path.realpath(path)
    descriptor = os.open(resolved, os.O_RDONLY | NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or info.st_size > maximum):
            raise ValueError("System module configuration failed metadata or size checks")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("System module configuration exceeds its size limit")
        return data.decode("utf-8", "strict")
    finally:
        os.close(descriptor)


def module_profile_configuration(deadline):
    """List bounded system shell snippets as data, without sourcing anything.

    Sites often set MODULEPATH in a separate /etc/profile.d file rather than
    the distribution's modules.sh. Directory and later file reads retain the
    strict root-owned system trust policy, independent of metadata tree policy.
    """
    paths, observations, truncated, count = [], [], False, 0
    for directory in MODULE_PROFILE_DIRECTORIES:
        if time.monotonic() >= deadline:
            truncated = True
            break
        try:
            trusted_system_path(directory, directory=True)
            with os.scandir(directory) as entries:
                for entry in entries:
                    if count >= 128 or len(paths) >= 48 or time.monotonic() >= deadline:
                        truncated = True
                        break
                    count += 1
                    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.sh", entry.name):
                        # Each file/link is separately audited before its bounded
                        # read. No directory aliases are recursively traversed.
                        paths.append(directory + "/" + entry.name)
            observations.append({"path": directory, "status": "listed"})
        except (OSError, ValueError):
            observations.append({"path": directory, "status": "unsafe_or_unavailable"})
    return sorted(paths), observations, truncated


def module_configuration_reference(path):
    """Only conventional absolute module initialization/configuration paths."""
    if not isinstance(path, str) or any(character in path for character in "$`;&|<>(){}\\*?["):
        return False
    try:
        discovery_path(path)
    except ValueError:
        return False
    if path.startswith(("/home/", "/users/", "/root/")):
        return False
    basename = os.path.basename(path)
    return (basename in {"modulespath", ".modulespath", "initrc"}
            or ("/init/" in path and basename in {"bash", "profile", "profile.sh", "sh"})
            or (path.startswith("/etc/profile.d/") and basename.endswith(".sh")))


def module_initializer_candidate(observed):
    """Inspect a known system reference and its canonical initializer metadata.

    Root-owned references may point into a site-admin software installation.
    That makes a useful human recipe candidate, never permission to source it.
    No initializer contents are read and no installed interpreter is executed.
    """
    observed = discovery_path(observed)
    current = "/"
    for component in observed.lstrip("/").split("/"):
        current = os.path.join(current, component)
        info = os.lstat(current)
        if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
            raise ValueError("Known initializer reference must be system-owned and protected")
    reference = os.lstat(observed)
    if not (stat.S_ISREG(reference.st_mode) or stat.S_ISLNK(reference.st_mode)):
        raise ValueError("Initializer reference is not a regular file or system link")
    original_target = discovery_path(os.path.realpath(observed))
    targets = [original_target]
    # Lmod's system profile is often the site's root-owned link. The adjacent
    # shell-specific initializer is the explicit Bash recipe candidate.
    if original_target.endswith("/init/profile"):
        targets.insert(0, os.path.dirname(original_target) + "/bash")
    for target in targets:
        directory = descriptor = None
        try:
            directory = open_metadata_module_root(os.path.dirname(target), allow_group_writable=True)
            descriptor = os.open(os.path.basename(target), getattr(os, "O_PATH", os.O_RDONLY) | NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o002:
                raise ValueError("Initializer candidate is not a protected regular file")
            try:
                trusted_system_path(target, directory=False)
                root_trusted = True
            except (OSError, ValueError):
                root_trusted = False
            return {"observed_path": observed, "observed_canonical_path": original_target,
                    "canonical_path": target, "source": "known_system_initializer_reference",
                    "owner_uid": info.st_uid, "group_gid": info.st_gid,
                    "mode": oct(stat.S_IMODE(info.st_mode)), "root_trusted": root_trusted,
                    "verified_on_compute": False}
        except (OSError, ValueError):
            continue
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                os.close(directory)
    raise ValueError("Initializer target metadata is unsafe or unavailable")


def literal_module_paths(text, *, path_file=False):
    """Extract literal path data only; shell syntax/expansions are never run.

    Conditional assignments are candidates, not evaluated shell state. The
    result is suitable only for a provisional filename catalog.
    """
    import shlex
    paths, references, unsupported = [], [], 0
    for line in text.replace("\\\n", "").splitlines():
        try:
            fields = shlex.split(line, comments=True, posix=True)
        except ValueError:
            unsupported += 1
            continue
        if not fields:
            continue
        if fields[0] == "export":
            fields = fields[1:]
        if not path_file and fields and fields[0] in {".", "source"}:
            if len(fields) == 2 and module_configuration_reference(fields[1]):
                references.append(fields[1])
            continue
        if not path_file and fields[:2] == ["module", "unuse"]:
            # Shell branch ordering/state is deliberately not interpreted. A
            # global subtraction could hide paths still active in another
            # branch. Keep a provisional union and disclose the uncertainty.
            unsupported += 1
            continue
        # Environment Modules initrc is a Tcl modulefile, but only literal
        # path declarations are read here. Never evaluate Tcl, defaults or rc.
        if not path_file and fields and fields[:2] == ["module", "use"]:
            values = fields[2:]
            if values and values[0] in {"-a", "--append", "-p", "--prepend"}:
                values = values[1:]
        elif not path_file and fields and fields[0] in {"prepend-path", "append-path"} and fields[1:2] == ["MODULEPATH"]:
            values = fields[2:]
        elif path_file:
            values = fields
        else:
            if not fields or not re.match(r"(?:MODULEPATH|LMOD_SITE_MODULEPATH|MODULEPATH_ROOT|LMOD_MODULEPATH_INIT|MODULEPATH_INIT)=", fields[0]):
                continue
            if len(fields) != 1:
                unsupported += 1
                continue
            variable, _, value = fields[0].partition("=")
            if any(character in value for character in "$`;&|<>(){}\\"):
                unsupported += 1
                continue
            if variable in {"LMOD_MODULEPATH_INIT", "MODULEPATH_INIT"}:
                if value.startswith("/") and os.path.basename(value) in {".modulespath", "modulespath"}:
                    references.append(value)
                continue
            values = [value + "/*" if variable == "MODULEPATH_ROOT" else value]
        for value in values:
            if any(character in value for character in "$`;&|<>(){}\\"):
                unsupported += 1
                continue
            for path in value.split(":"):
                try:
                    discovery_path(path)
                except ValueError:
                    unsupported += 1
                    continue
                paths.append(path)
    return paths, references, unsupported


def automatic_module_inventory(options):
    """Find the site catalog from trusted configuration data without evaluation.

    In particular, do not source /etc/profile: even a trusted initializer can
    load defaults or evaluate unaudited modulerc files. Literal assignments and
    Modules/Lmod path-list files provide bounded, provisional candidate roots.
    """
    candidates, provenance, warnings, configuration_sources = [], [], [], []
    group_managed_roots = []
    configuration_directories = []
    truncated, unsupported, total_bytes, entries = False, 0, 0, 0
    deadline = time.monotonic() + 2
    initializers = [options["module_init"]] if options.get("module_init") else list(MODULE_INITIALIZERS)
    chosen_initializer = None
    initializer_candidates = []
    for initializer in initializers:
        try:
            initializer_candidates.append(module_initializer_candidate(initializer))
        except (OSError, ValueError):
            pass
        try:
            trusted_system_path(initializer, directory=False)
            if chosen_initializer is None:
                chosen_initializer = initializer
        except (OSError, ValueError):
            pass
    if "module_paths" in options:
        sources = [(path, "configured_module_paths") for path in options["module_paths"]]
    else:
        sources = []
        pending = list(dict.fromkeys([*MODULE_PATH_CONFIGURATION_FILES, *initializers]))
        snippets, configuration_directories, listing_truncated = module_profile_configuration(deadline)
        pending.extend(snippets)
        truncated = truncated or listing_truncated
        for initializer in initializers:
            pending.append(os.path.dirname(os.path.realpath(initializer)) + "/.modulespath")
        seen = set()
        while pending and len(seen) < 64 and total_bytes < 131072 and time.monotonic() < deadline:
            filename = pending.pop(0)
            if filename in seen:
                continue
            seen.add(filename)
            try:
                text = read_trusted_module_configuration(filename, min(65536, 131072 - total_bytes))
            except (OSError, ValueError, UnicodeError) as error:
                configuration_sources.append({"path": filename,
                    "status": "not_found" if isinstance(error, FileNotFoundError) else "unsafe_unavailable_or_size_limited"})
                continue
            total_bytes += len(text.encode("utf-8"))
            configuration_sources.append({"path": filename, "status": "read", "size_bytes": len(text.encode("utf-8"))})
            paths, references, omitted = literal_module_paths(text, path_file=os.path.basename(filename) in {".modulespath", "modulespath"})
            unsupported += omitted
            sources.extend((path, filename) for path in paths)
            pending.extend(references)
        if pending:
            truncated = True
        # Site path data takes precedence. Adding a parent/default tree as well
        # can invent prefixed names (Core/python3 instead of python3).
        if not sources:
            sources.extend((path, "standard_module_directory") for path in SYSTEM_MODULE_PATHS)
    for candidate, source in sources:
        root_declared = source not in {"configured_module_paths", "standard_module_directory"}
        if len(candidates) >= 64 or time.monotonic() >= deadline:
            truncated = True
            break
        roots = [candidate]
        home = pwd.getpwuid(os.getuid()).pw_dir.rstrip("/")
        if (candidate == home or candidate.startswith(home + "/")
                or candidate in {"/home", "/users", "/root"} or candidate.startswith(("/home/", "/users/", "/root/"))):
            unsupported += 1
            continue
        if any(character in candidate for character in "*?["):
            # Only one final '*' component is supported, with bounded iteration.
            # A full glob engine can traverse arbitrarily large directory trees.
            if not candidate.endswith("/*") or any(character in candidate[:-2] for character in "*?["):
                unsupported += 1
                continue
            roots = []
            descriptor = None
            try:
                parent = discovery_path(os.path.realpath(candidate[:-2]))
                descriptor = open_metadata_module_root(parent, deadline=deadline, allow_group_writable=root_declared)
                with os.scandir(descriptor) as directory:
                    for entry in directory:
                        if entries >= 4096 or len(roots) >= 64 or time.monotonic() >= deadline:
                            truncated = True
                            break
                        entries += 1
                        if entry.is_dir(follow_symlinks=False):
                            roots.append(parent + "/" + entry.name)
            except (OSError, ValueError):
                warnings.append("A literal wildcard module root was unsafe, unavailable or outside discovery bounds: " + candidate)
                continue
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        for root in roots:
            if len(candidates) >= 64 or time.monotonic() >= deadline:
                truncated = True
                break
            try:
                resolved = discovery_path(os.path.realpath(root))
            except (OSError, ValueError):
                unsupported += 1
                continue
            if resolved not in candidates:
                candidates.append(resolved)
                provenance.append({"path": root, "resolved_path": resolved, "source": source})
            if root_declared and resolved not in group_managed_roots:
                group_managed_roots.append(resolved)
    # Validate the complete intended search path independently of recursive
    # enumeration. A cold/large earlier tree must not hide later valid roots
    # from the separately authorized availability command.
    validated_paths, validation_skipped, validation_truncated = [], [], False
    validation_deadline = time.monotonic() + 20
    for root in candidates:
        if time.monotonic() >= validation_deadline:
            validation_truncated = True
            validation_skipped.append({"path": root, "status": "not_scanned_limit"})
            continue
        descriptor = None
        try:
            descriptor = open_metadata_module_root(root, deadline=validation_deadline,
                allow_group_writable=root in group_managed_roots)
            validated_paths.append(root)
        except (OSError, ValueError) as error:
            limited = isinstance(error, TimeoutError)
            validation_truncated = validation_truncated or limited
            validation_skipped.append({"path": root, "status": "not_scanned_limit" if limited else
                "not_found" if isinstance(error, FileNotFoundError) else "unsafe_or_inaccessible"})
        finally:
            if descriptor is not None:
                os.close(descriptor)
    scan_options = {"module_paths": validated_paths, "group_managed_roots": group_managed_roots,
                    "automatic_limits": True}
    if chosen_initializer:
        scan_options["module_init"] = chosen_initializer
    result = metadata_module_inventory(scan_options)
    if chosen_initializer:
        # Command execution keeps the pre-existing strict root trust policy,
        # independent of the provisional recipe candidate selected below.
        result["availability_init_path"] = chosen_initializer
    if initializer_candidates:
        preferred = initializer_candidates[0]
        result.update(init_path=preferred["canonical_path"],
            observed_init_path=preferred["observed_path"], canonical_init_path=preferred["canonical_path"],
            initializer_status="metadata_verified" if preferred["root_trusted"] else "site_metadata_candidate",
            initializer_candidates=initializer_candidates, initializer_verified_on_compute=False)
        if not preferred["root_trusted"]:
            warnings.append("Canonical site initializer is a metadata-only recipe candidate requiring human review; it was not executed or verified on compute nodes.")
    result.update(requested_inventory_mode="auto", path_source="configured" if "module_paths" in options else "trusted_system_configuration",
        source="automatic_system_modulepath_metadata", path_provenance=provenance,
        intended_module_paths=candidates, validated_module_paths=validated_paths,
        module_path_validation_truncated=validation_truncated,
        path_semantics="literal_candidate_union",
        configuration_bytes_read=total_bytes, configuration_sources=configuration_sources,
        configuration_directories=configuration_directories,
        unsupported_configuration_lines=unsupported,
        loaded=[], loaded_state="not_observed")
    if validation_skipped:
        result["skipped_paths"].extend(validation_skipped)
        result["partial"] = True
        warnings.append("Some declared module roots failed independent path validation; inspect skipped_paths.")
    if validation_truncated:
        result["truncated"] = result["partial"] = True
        warnings.append("Module search-path validation reached its twenty-second bound; availability will not run with a shortened path.")
    if unsupported:
        result["partial"] = True
        warnings.append("Dynamic or unsupported configuration syntax was not evaluated; candidate roots may be incomplete.")
    if warnings:
        result["partial"] = True
    if truncated:
        result["truncated"] = result["partial"] = True
        warnings.append("Automatic path discovery reached a configuration, time, directory-entry or path-count bound.")
    result["warnings"].extend(warnings)
    result["explanation"] = "Provisional catalog from the union of literal system MODULEPATH/module-use declarations and regular module-file metadata. Conditional branches and module unuse are not interpreted. System shell startup, module files and rc files were not executed; aliases, loaded state and compute-node availability are unverified."
    return result


def metadata_module_directory_permissions(info, path, *, final, ancestor_warnings, allow_group_writable=False):
    if not info.st_mode & 0o022:
        return
    if allow_group_writable and not info.st_mode & 0o002:
        # Only auto roots declared by strict root-owned system configuration get
        # this name-only exception. It never authorizes evaluation or file reads.
        warning = {"path": path, "uid": info.st_uid, "gid": info.st_gid,
                   "mode": oct(stat.S_IMODE(info.st_mode)), "reason": "group_managed_metadata_only"}
        if warning not in ancestor_warnings and len(ancestor_warnings) < 64:
            ancestor_warnings.append(warning)
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


def open_metadata_module_root(path, *, deadline=None, ancestor_warnings=None, allow_group_writable=False):
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
        metadata_module_directory_permissions(os.fstat(fd), "/", final=False, ancestor_warnings=ancestor_warnings,
                                              allow_group_writable=allow_group_writable)
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
                                                  ancestor_warnings=ancestor_warnings, allow_group_writable=allow_group_writable)
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
              "group_writable_entries": 0,
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
    # Automatic system catalogs can be much larger than an explicitly selected
    # small tree. These are internal fixed bounds, not caller-supplied budgets.
    automatic = options.get("automatic_limits", False)
    max_entries, max_directories, max_names = (32768, 4096, 8192) if automatic else (4096, 256, 512)
    max_depth, seconds = (12, 20) if automatic else (8, 2)
    result["limits"] = {"entries": max_entries, "directories": max_directories, "names": max_names,
                        "depth": max_depth, "seconds": seconds}
    group_managed_roots = set(options.get("group_managed_roots", []))
    result["write_policy"] = "root_declared_group_managed_metadata" if group_managed_roots else "strict_metadata"
    deadline = time.monotonic() + seconds
    names = set()
    for root in dict.fromkeys(options["module_paths"]):
        observation = {"path": root, "status": "unavailable"}
        result["roots"].append(observation)
        allow_group_writable = root in group_managed_roots
        if result["visited_entries"] >= max_entries or result["visited_directories"] >= max_directories or len(names) >= max_names or time.monotonic() >= deadline:
            result["truncated"] = result["partial"] = True
            observation["status"] = "not_scanned_limit"
            result["skipped_paths"].append({"path": root, "status": "not_scanned_limit"})
            continue
        try:
            root_fd = open_metadata_module_root(root, deadline=deadline, ancestor_warnings=result["ancestor_warnings"],
                                                allow_group_writable=allow_group_writable)
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
                if result["visited_entries"] >= max_entries or result["visited_directories"] >= max_directories or len(names) >= max_names or time.monotonic() >= deadline:
                    result["truncated"] = result["partial"] = True
                    observation["status"] = "truncated"
                    break
                directory, prefix, depth = pending.pop()
                try:
                    # Check the opened inode again, not just the earlier entry.
                    directory_mode = os.fstat(directory).st_mode
                    if directory_mode & 0o022 and not (allow_group_writable and not directory_mode & 0o002):
                        result["skipped"]["unsafe_permissions"] += 1
                        result["partial"] = True
                        continue
                    if directory_mode & 0o020:
                        result["group_writable_entries"] += 1
                    result["visited_directories"] += 1
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if result["visited_entries"] >= max_entries or len(names) >= max_names or time.monotonic() >= deadline:
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
                            if info.st_mode & 0o022 and not (allow_group_writable and not info.st_mode & 0o002):
                                result["skipped"]["unsafe_permissions"] += 1
                                result["partial"] = True
                                continue
                            relative = prefix + "/" + entry.name if prefix else entry.name
                            if stat.S_ISDIR(info.st_mode):
                                if depth >= max_depth:
                                    result["skipped"]["depth_limit"] += 1
                                    result["truncated"] = result["partial"] = True
                                    continue
                                # Pending handles are bounded too; do not open
                                # thousands of siblings before visiting any.
                                if result["visited_directories"] + len(pending) >= max_directories:
                                    result["truncated"] = result["partial"] = True
                                    observation["status"] = "truncated"
                                    continue
                                child = os.open(entry.name, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=directory)
                                pending.append((child, relative, depth + 1))
                            elif stat.S_ISREG(info.st_mode):
                                if info.st_mode & 0o020:
                                    result["group_writable_entries"] += 1
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
        result["warnings"].append("Catalog roots cross shared writable ancestors or group-managed software trees. This provisional metadata exception grants no authority to evaluate module code; inspect ancestor_warnings.")
    if group_managed_roots:
        result["warnings"].append("Root-declared site software trees may be software-group writable. Candidate names are untrusted metadata, not permission to execute module files.")
    return result


def augment_module_availability(modules):
    """Owner-authorized fixed avail; it may evaluate site module/rc code.

    This permission is separate from root-owned declaration metadata. Neither
    arbitrary commands nor personal shell startup are accepted. Results retain
    their provenance instead of turning filename candidates into tested names.
    """
    observation = {"enabled": True, "status": "unavailable", "available": [],
                   "source": "module_terse_avail", "scope": "login_node", "truncated": False,
                   "module_code_evaluation_authorized": True}
    modules["availability"] = observation
    modules["metadata_available"] = list(modules["available"])
    initializer = modules.get("availability_init_path", modules.get("init_path"))
    paths = modules.get("validated_module_paths", modules.get("module_paths", []))
    observation["module_paths"] = list(paths)
    if modules.get("module_path_validation_truncated"):
        observation["reason"] = "full_modulepath_validation_incomplete"
        modules["partial"] = True
        return
    if not initializer or not paths:
        observation["reason"] = "trusted_initializer_or_module_paths_unavailable"
        modules["partial"] = True
        return
    try:
        trusted_system_path(initializer, directory=False)
        import shlex
        probe = software_command(["/bin/bash", "--noprofile", "--norc", "-c",
            ". " + shlex.quote(initializer) + " >/dev/null 2>&1 && export MODULEPATH=" +
            shlex.quote(":".join(paths)) + " && module --terse avail"],
            extra_env={"MODULEPATH": ":".join(paths), "LMOD_TERSE_DECORATIONS": "no"},
            timeout=8, max_output_bytes=262144)
        names = available_modules(probe["stdout"] + "\n" + probe["stderr"])
        observation.update(status="available" if probe["returncode"] == 0 else "unavailable",
                           available=names[:8192], truncated=probe["truncated"] or len(names) > 8192,
                           returncode=probe["returncode"])
        if probe["returncode"] == 0:
            union = sorted(set(modules["available"]) | set(observation["available"]))
            modules["available"] = union[:8192]
            if union:
                modules["status"] = "available"
            if len(union) > 8192:
                modules["truncated"] = modules["partial"] = True
        else:
            observation["reason"] = "fixed_availability_command_failed"
            modules["partial"] = True
        if observation["truncated"]:
            modules["partial"] = modules["truncated"] = True
            modules["warnings"].append("Authorized module availability reached its eight-second, output-byte or name bound.")
    except (OSError, ValueError, subprocess.SubprocessError):
        observation["reason"] = "fixed_availability_command_unavailable"
        modules["partial"] = True
    modules["warnings"].append("Owner-authorized module availability may evaluate site module/rc code under the remote user; no personal shell startup is sourced.")
