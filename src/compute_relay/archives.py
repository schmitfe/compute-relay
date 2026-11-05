"""Bounded ZIP helpers, also shipped as a dependency-free compute-job zipapp."""

from contextlib import contextmanager
from importlib.resources import files
import json
import os
from pathlib import PurePosixPath
import secrets
import shutil
import stat
import zipfile

from .local_filesystem import PRIVATE_NAMES, absolute_path, check_path, ensure_directory, parent_descriptor, portable_name
from .transfer import CHUNK_BYTES, MAX_BUNDLE_BYTES, MAX_FILE_BYTES


MAX_FILES = 10000
MAX_METADATA_BYTES = 16 * 1024**2


class _ZipReader:
    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, size=-1):
        # ZipFile reads the entire central directory before exposing entry counts.
        if size > MAX_METADATA_BYTES:
            raise ValueError("Archive metadata exceeds the parsing limit.")
        data = self.stream.read(MAX_METADATA_BYTES + 1 if size < 0 else size)
        if len(data) > MAX_METADATA_BYTES:
            raise ValueError("Archive metadata exceeds the parsing limit.")
        return data


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _relative(name):
    if (not isinstance(name, str) or not name or len(name) > 1024
            or "\\" in name or ":" in name or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or name.startswith("/") or len(name.split("/")) > 17
            or any(portable_name(part) in {"", ".", ".."} | PRIVATE_NAMES for part in name.split("/"))):
        raise ValueError("Choose canonical relative paths outside private configuration directories.")
    return name


def _root(directory):
    root = absolute_path(directory)
    check_path(root, directory=True)
    if not root.is_dir():
        raise ValueError("The selected directory must exist.")
    return root


@contextmanager
def _source(path):
    with parent_descriptor(path) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid():
                raise ValueError("Archive inputs must be ordinary owned files without links.")
            yield stream, before
            if (_identity(before) != _identity(os.fstat(stream.fileno()))
                    or _identity(before) != _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False))):
                raise ValueError("An archive input changed while it was being read.")


@contextmanager
def _output(path):
    with parent_descriptor(path) as parent:
        temporary = ".compute-relay-archive-" + secrets.token_hex(12)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                yield stream
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
        finally:
            os.unlink(temporary, dir_fd=parent)


def _limits(max_files, max_bytes):
    if (type(max_files) is not int or not 1 <= max_files <= MAX_FILES
            or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BUNDLE_BYTES):
        raise ValueError("Archive limits must be positive and within 10,000 entries / 64 GiB.")


def pack_zip(directory, paths, output, *, max_files=MAX_FILES, max_bytes=MAX_BUNDLE_BYTES):
    """Pack explicit relative files; stream bounded data and never overwrite output."""
    _limits(max_files, max_bytes)
    root = _root(directory)
    destination = root / _relative(output)
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= max_files:
        raise ValueError("Select between one and the maximum number of explicit files.")
    names = sorted(_relative(name) for name in paths)
    if len({portable_name(name) for name in names}) != len(names) or portable_name(output) in {portable_name(name) for name in names}:
        raise ValueError("Archive inputs must be unique and exclude the output path.")
    total = 0
    with _output(destination) as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                with _source(root / name) as (source, before):
                    if before.st_size > min(MAX_FILE_BYTES, max_bytes - total):
                        raise ValueError("Archive inputs exceed the byte limit.")
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    mode = 0o700 if before.st_mode & 0o111 else 0o600
                    info.external_attr = (stat.S_IFREG | mode) << 16
                    copied = 0
                    with archive.open(info, "w", force_zip64=True) as target:
                        while chunk := source.read(CHUNK_BYTES):
                            copied += len(chunk)
                            if copied > before.st_size:
                                raise ValueError("An archive input grew while it was being read.")
                            target.write(chunk)
                    if copied != before.st_size:
                        raise ValueError("An archive input changed while it was being read.")
                    total += copied
    return {"path": output, "files": len(names), "expanded_bytes": total}


def unpack_zip(directory, archive, output, *, max_files=MAX_FILES, max_bytes=MAX_BUNDLE_BYTES):
    """Extract checked ZIP members into a new private directory; remove it on failure."""
    _limits(max_files, max_bytes)
    root = _root(directory)
    destination = root / _relative(output)
    total, count = 0, 0
    with parent_descriptor(destination) as parent:
        created = False
        try:
            with _source(root / _relative(archive)) as (source, _), zipfile.ZipFile(_ZipReader(source)) as bundle:
                entries = bundle.infolist()
                if len(entries) > max_files:
                    raise ValueError("Archive exceeds the entry limit.")
                names, regular = set(), set()
                for entry in entries:
                    name = _relative(entry.orig_filename[:-1] if entry.is_dir() else entry.orig_filename)
                    name = portable_name(name)
                    kind = stat.S_IFMT(entry.external_attr >> 16)
                    if (name in names or entry.flag_bits & 1
                            or kind not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG)
                            or entry.is_dir() and entry.file_size):
                        raise ValueError("Archive contains duplicate, encrypted or non-regular entries.")
                    names.add(name)
                    if not entry.is_dir():
                        regular.add(name)
                    total += entry.file_size
                    if entry.file_size > MAX_FILE_BYTES or total > max_bytes:
                        raise ValueError("Archive exceeds the expanded byte limit.")
                if any(str(ancestor) in regular for name in names for ancestor in PurePosixPath(name).parents):
                    raise ValueError("Archive contains conflicting file and directory paths.")
                os.mkdir(destination.name, 0o700, dir_fd=parent)
                created = True
                for entry in entries:
                    path = destination / entry.filename
                    ensure_directory(path if entry.is_dir() else path.parent, [])
                    if entry.is_dir():
                        continue
                    copied = 0
                    with bundle.open(entry) as content, _output(path) as target:
                        while chunk := content.read(CHUNK_BYTES):
                            copied += len(chunk)
                            if copied > entry.file_size:
                                raise ValueError("Archive entry exceeds its declared size.")
                            target.write(chunk)
                        if copied != entry.file_size:
                            raise ValueError("Archive entry has an incomplete payload.")
                    if entry.external_attr >> 16 & 0o111:
                        path.chmod(0o700)
                    count += 1
        except BaseException:
            if created:
                shutil.rmtree(destination.name, dir_fd=parent)
            raise
    return {"path": output, "files": count, "expanded_bytes": total}


def write_archive_helper(directory, output="relay-files.pyz"):
    """Export these same helpers for Python on a compute node; no broker setup needed."""
    destination = _root(directory) / _relative(output)
    package = files("compute_relay")
    with _output(destination) as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("compute_relay/__init__.py", "")
            for module in ("archives.py", "local_filesystem.py", "transfer.py"):
                archive.writestr("compute_relay/" + module, package.joinpath(module).read_bytes())
            archive.writestr("__main__.py", "from compute_relay.archives import main\nmain()\n")
    return {"path": output}


def register_commands(actions):
    for action in ("zip", "unzip", "archive-helper"):
        parser = actions.add_parser(action, help={"zip": "Pack explicitly selected files into ZIP",
            "unzip": "Extract a ZIP into a new directory", "archive-helper": "Export portable ZIP commands for compute jobs"}[action])
        parser.add_argument("--directory", default=".")
        parser.add_argument("--output", required=True)
        if action == "zip":
            parser.add_argument("paths", nargs="+")
        elif action == "unzip":
            parser.add_argument("archive")
        if action != "archive-helper":
            parser.add_argument("--max-files", type=int, default=MAX_FILES)
            parser.add_argument("--max-bytes", type=int, default=MAX_BUNDLE_BYTES)
        parser.set_defaults(run=main, file_action=action)


def main(args=None):
    if args is None:
        import argparse
        parser = argparse.ArgumentParser(description="Portable Compute Relay ZIP helpers")
        register_commands(parser.add_subparsers(required=True))
        args = parser.parse_args()
    try:
        if args.file_action == "archive-helper":
            result = write_archive_helper(args.directory, args.output)
        else:
            operation = pack_zip if args.file_action == "zip" else unpack_zip
            source = args.paths if args.file_action == "zip" else args.archive
            result = operation(args.directory, source, args.output, max_files=args.max_files, max_bytes=args.max_bytes)
    except Exception:
        raise SystemExit("Archive operation failed. Check relative paths, ordinary input files, limits and a fresh output destination.") from None
    print(json.dumps(result, sort_keys=True))
