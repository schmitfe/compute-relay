"""The same bounded archive code runs locally and in a standalone compute job."""

import json
import os
import stat
import struct
import subprocess
import sys
import zipfile

import pytest

from compute_relay import archives
from compute_relay.cli import main


def test_explicit_roundtrip_streams_nested_files_and_preserves_owner_execute(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.py").write_bytes(b"print('hello')\n")
    (tmp_path / "src/main.py").chmod(0o755)
    data = bytes(range(256)) * 8192
    (tmp_path / "data.bin").write_bytes(data)
    (tmp_path / "unselected.key").write_bytes(b"never packed")
    receipt = archives.pack_zip(tmp_path, ["src/main.py", "data.bin"], "source.zip")
    assert receipt == {"path": "source.zip", "files": 2, "expanded_bytes": len(data) + 15}
    assert (tmp_path / "source.zip").stat().st_mode & 0o777 == 0o600
    with zipfile.ZipFile(tmp_path / "source.zip") as bundle:
        assert bundle.namelist() == ["data.bin", "src/main.py"]
    assert archives.unpack_zip(tmp_path, "source.zip", "extracted")["files"] == 2
    assert (tmp_path / "extracted/data.bin").read_bytes() == data
    assert (tmp_path / "extracted/src/main.py").read_bytes() == (tmp_path / "src/main.py").read_bytes()
    assert (tmp_path / "extracted/src/main.py").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("name", ["../escape", "/absolute", "dir/../escape", "dir//file", "./file",
    "C:/file", "dir\\file", "control\nfile", "nul\0file", ".ssh/key", "src/.compute-relay/project.key",
    ".SSH/key", "src/.Compute-Relay/project.key"])
def test_rejects_unsafe_names_before_creating_any_output(tmp_path, name):
    with zipfile.ZipFile(tmp_path / "source.zip", "w") as bundle:
        bundle.writestr(name.replace("\0", "X"), b"unsafe")
    if "\0" in name:
        path = tmp_path / "source.zip"
        path.write_bytes(path.read_bytes().replace(name.replace("\0", "X").encode(), name.encode()))
    with pytest.raises(ValueError):
        archives.unpack_zip(tmp_path, "source.zip", "extracted")
    assert not (tmp_path / "extracted").exists()
    with pytest.raises(ValueError):
        archives.pack_zip(tmp_path, [name], "packed.zip")
    assert not (tmp_path / "packed.zip").exists()


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR])
def test_rejects_nonregular_zip_members(tmp_path, kind):
    info = zipfile.ZipInfo("unsafe")
    info.external_attr = (kind | 0o777) << 16
    with zipfile.ZipFile(tmp_path / "source.zip", "w") as bundle:
        bundle.writestr(info, b"target")
    with pytest.raises(ValueError, match="non-regular"):
        archives.unpack_zip(tmp_path, "source.zip", "extracted")
    assert not (tmp_path / "extracted").exists()


@pytest.mark.parametrize("names", [("a", "a"), ("a", "a/file"), ("a/file", "a"), ("a/", "a"),
    ("A", "a"), ("caf\u00e9", "cafe\u0301"), ("A", "a/file")])
def test_rejects_duplicate_and_conflicting_paths(tmp_path, names):
    with zipfile.ZipFile(tmp_path / "source.zip", "w") as bundle:
        for name in names:
            if name in bundle.namelist():
                with pytest.warns(UserWarning):
                    bundle.writestr(name, b"data")
            else:
                bundle.writestr(name, b"" if name.endswith("/") else b"data")
    with pytest.raises(ValueError):
        archives.unpack_zip(tmp_path, "source.zip", "extracted")
    assert not (tmp_path / "extracted").exists()


def test_accepts_explicit_directories_and_empty_archives(tmp_path):
    with zipfile.ZipFile(tmp_path / "source.zip", "w") as bundle:
        bundle.mkdir("empty/")
        bundle.writestr("data/values.csv", "1,2\n")
    archives.unpack_zip(tmp_path, "source.zip", "extracted")
    assert (tmp_path / "extracted/empty").is_dir()
    assert (tmp_path / "extracted/data/values.csv").read_text() == "1,2\n"
    with zipfile.ZipFile(tmp_path / "empty.zip", "w"):
        pass
    assert archives.unpack_zip(tmp_path, "empty.zip", "nothing")["files"] == 0


def test_existing_outputs_and_symlinked_parents_are_never_overwritten(tmp_path):
    (tmp_path / "input.txt").write_text("kept")
    archives.pack_zip(tmp_path, ["input.txt"], "source.zip")
    original = (tmp_path / "source.zip").read_bytes()
    with pytest.raises(FileExistsError):
        archives.pack_zip(tmp_path, ["input.txt"], "source.zip")
    assert (tmp_path / "source.zip").read_bytes() == original
    (tmp_path / "existing").mkdir()
    (tmp_path / "existing/keep").write_text("untouched")
    with pytest.raises(FileExistsError):
        archives.unpack_zip(tmp_path, "source.zip", "existing")
    assert (tmp_path / "existing/keep").read_text() == "untouched"
    (tmp_path / "alias").symlink_to(tmp_path / "existing", target_is_directory=True)
    with pytest.raises(OSError):
        archives.unpack_zip(tmp_path, "source.zip", "alias/escaped")
    assert not (tmp_path / "existing/escaped").exists()


@pytest.mark.parametrize("link", ["symbolic", "hard"])
def test_packing_does_not_follow_links(tmp_path, link):
    (tmp_path / "input.txt").write_text("private")
    if link == "symbolic":
        (tmp_path / "link").symlink_to(tmp_path / "input.txt")
    else:
        os.link(tmp_path / "input.txt", tmp_path / "link")
    with pytest.raises((ValueError, OSError)):
        archives.pack_zip(tmp_path, ["link"], "packed.zip")
    assert not (tmp_path / "packed.zip").exists()
    assert not list(tmp_path.glob(".compute-relay-archive-*"))


@pytest.mark.parametrize("limits", [{"max_files": 1}, {"max_bytes": 5}])
def test_count_and_expanded_size_limits_protect_both_operations(tmp_path, limits):
    for name in ("one", "two"):
        (tmp_path / name).write_bytes(b"1234")
    archives.pack_zip(tmp_path, ["one", "two"], "source.zip")
    with pytest.raises(ValueError):
        archives.pack_zip(tmp_path, ["one", "two"], "limited.zip", **limits)
    with pytest.raises(ValueError):
        archives.unpack_zip(tmp_path, "source.zip", "limited", **limits)
    assert not (tmp_path / "limited.zip").exists() and not (tmp_path / "limited").exists()


def test_zip_metadata_is_bounded_before_zipfile_allocates_it(tmp_path, monkeypatch):
    with zipfile.ZipFile(tmp_path / "source.zip", "w") as bundle:
        for number in range(100):
            bundle.writestr(str(number), b"")
    monkeypatch.setattr(archives, "MAX_METADATA_BYTES", 1024)
    with pytest.raises(ValueError, match="metadata"):
        archives.unpack_zip(tmp_path, "source.zip", "extracted")
    assert not (tmp_path / "extracted").exists()


def test_bad_crc_discards_partial_extraction_and_preserves_inputs(tmp_path):
    with zipfile.ZipFile(tmp_path / "source.zip", "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr("first", b"first payload")
        bundle.writestr("second", b"second payload")
        offset = bundle.getinfo("second").header_offset
    damaged = bytearray((tmp_path / "source.zip").read_bytes())
    name_size, extra_size = struct.unpack_from("<HH", damaged, offset + 26)
    damaged[offset + 30 + name_size + extra_size] ^= 1
    (tmp_path / "source.zip").write_bytes(damaged)
    with pytest.raises(zipfile.BadZipFile):
        archives.unpack_zip(tmp_path, "source.zip", "extracted")
    assert not (tmp_path / "extracted").exists()
    assert (tmp_path / "source.zip").read_bytes() == damaged


def test_input_replacement_prevents_archive_publication(tmp_path, monkeypatch):
    (tmp_path / "input.txt").write_text("before")
    original = zipfile.ZipFile.open
    def replace_after_read(bundle, *args, **kwargs):
        result = original(bundle, *args, **kwargs)
        (tmp_path / "replacement").write_text("after")
        (tmp_path / "replacement").replace(tmp_path / "input.txt")
        return result
    monkeypatch.setattr(zipfile.ZipFile, "open", replace_after_read)
    with pytest.raises(ValueError, match="changed"):
        archives.pack_zip(tmp_path, ["input.txt"], "source.zip")
    assert not (tmp_path / "source.zip").exists()


def test_cli_and_exported_helper_work_without_workspace_credentials_or_installed_package(tmp_path, capsys):
    from compute_relay.sdk import pack_zip, unpack_zip, write_archive_helper
    assert pack_zip is archives.pack_zip and unpack_zip is archives.unpack_zip
    assert write_archive_helper is archives.write_archive_helper
    (tmp_path / "input.txt").write_text("payload")
    main(["files", "zip", "--directory", str(tmp_path), "--output", "source.zip", "input.txt"])
    main(["files", "archive-helper", "--directory", str(tmp_path), "--output", "relay-files.pyz"])
    assert "payload" not in capsys.readouterr().out
    helper = tmp_path / "relay-files.pyz"
    with zipfile.ZipFile(helper) as bundle:
        assert set(bundle.namelist()) == {"__main__.py", "compute_relay/__init__.py",
            "compute_relay/archives.py", "compute_relay/local_filesystem.py", "compute_relay/transfer.py"}
    for arguments in (["unzip", "source.zip", "--output", "compute-work"],
            ["zip", "--directory", "compute-work", "--output", "result.zip", "input.txt"],
            ["unzip", "compute-work/result.zip", "--output", "collected"]):
        result = subprocess.run([sys.executable, "-I", "-S", str(helper), *arguments], cwd=tmp_path,
            capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["files"] == 1
    assert (tmp_path / "collected/input.txt").read_text() == "payload"
    assert not (tmp_path / ".compute-relay").exists()
