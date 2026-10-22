"""Local temporary directory metadata only; no SSH or real account home scan."""

import builtins
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from compute_broker.remote import connector_setup as setup


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(setup.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(home)))
    monkeypatch.chdir(home)
    return home


def test_identity_uses_canonical_account_home_instead_of_home_environment(fake_home, tmp_path, monkeypatch):
    alias = tmp_path / "home-alias"
    alias.symlink_to(fake_home, target_is_directory=True)
    monkeypatch.setattr(setup.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(alias)))
    monkeypatch.setenv("HOME", "/environment/path-must-not-be-used")
    assert setup.handle_request({"operation": "identity"}) == {"ok": True, "home": str(fake_home), "cwd": str(fake_home)}
    monkeypatch.setattr(setup.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(alias) + "/"))
    assert setup.handle_request({"operation": "identity"})["home"] == str(fake_home)


def test_browse_defaults_to_home_and_returns_only_real_child_directories(fake_home):
    (fake_home / "zeta").mkdir()
    (fake_home / '<a title="quote">').mkdir()
    (fake_home / "regular-file").write_text("private contents are never read")
    (fake_home / "alias").symlink_to(fake_home / "zeta", target_is_directory=True)
    response = setup.handle_request({"operation": "browse"})
    assert response == {"ok": True, "path": str(fake_home), "parent": str(fake_home.parent), "home": str(fake_home),
        "entries": [{"name": '<a title="quote">', "path": str(fake_home / '<a title="quote">')},
                    {"name": "zeta", "path": str(fake_home / "zeta")}], "writable": True, "truncated": False, "scanned_entries": 4}
    assert "private" not in json.dumps(response)


def test_browse_explicit_symlink_location_returns_canonical_path(fake_home, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(fake_home, target_is_directory=True)
    result = setup.handle_request({"operation": "browse", "path": str(alias)})
    assert result["ok"] and result["path"] == str(fake_home) and result["parent"] == str(fake_home.parent)


@pytest.mark.parametrize("path", ["relative", "/tmp/../etc", "/tmp/./folder", "//tmp", "/tmp/", "/tmp\x00name", "/tmp/\ud800"])
def test_browse_rejects_noncanonical_or_invalid_lexical_paths(path):
    assert setup.handle_request({"operation": "browse", "path": path}) == {"ok": False, "error_code": "INVALID_PATH"}


def test_browse_directory_count_scan_count_and_deadline_bounds_are_explicit(fake_home, monkeypatch):
    for name in ("one", "two", "three"):
        (fake_home / name).mkdir()
    monkeypatch.setattr(setup, "MAX_DIRECTORIES", 2)
    assert setup.handle_request({"operation": "browse"})["truncated"] is True
    assert len(setup.handle_request({"operation": "browse"})["entries"]) == 2
    monkeypatch.setattr(setup, "MAX_DIRECTORIES", 200)
    monkeypatch.setattr(setup, "MAX_SCAN_ENTRIES", 1)
    result = setup.handle_request({"operation": "browse"})
    assert result["truncated"] and result["scanned_entries"] == 1
    monkeypatch.setattr(setup, "SCAN_SECONDS", 0)
    result = setup.handle_request({"operation": "browse"})
    assert result["truncated"] and result["entries"] == []


def test_browse_output_budget_truncates_long_directory_names(fake_home, monkeypatch):
    for number in range(20):
        (fake_home / (str(number) + "\U0001f9ea" * 30)).mkdir()
    monkeypatch.setattr(setup, "MAX_RESPONSE_BYTES", 4096)
    result = setup.handle_request({"operation": "browse"})
    assert result["truncated"] and 0 < len(result["entries"]) < 20
    assert len(setup._encoded(result)) + 1 <= 4096


def test_browse_does_not_pull_an_extra_entry_beyond_its_scan_budget(fake_home, monkeypatch):
    (fake_home / "one").mkdir()
    (fake_home / "two").mkdir()
    original = setup.os.scandir
    pulled = 0

    class CountingScan:
        def __init__(self, fd):
            self.entries = original(fd)
        def __enter__(self):
            self.entries.__enter__()
            return self
        def __exit__(self, *args):
            return self.entries.__exit__(*args)
        def __next__(self):
            nonlocal pulled
            pulled += 1
            assert pulled <= 1
            return next(self.entries)

    monkeypatch.setattr(setup.os, "scandir", CountingScan)
    monkeypatch.setattr(setup, "MAX_SCAN_ENTRIES", 1)
    result = setup.handle_request({"operation": "browse"})
    assert result["truncated"] and result["scanned_entries"] == pulled == 1


def test_browse_skips_non_utf8_names_instead_of_emitting_lone_surrogates(fake_home):
    os.mkdir(os.fsencode(fake_home) + b"/invalid-\xff")
    result = setup.handle_request({"operation": "browse"})
    assert result["ok"] and result["entries"] == [] and result["truncated"]
    json.dumps(result).encode("utf-8", "strict")


def test_work_root_validation_does_not_create_missing_directories(fake_home):
    path = str(fake_home / "future" / "jobs")
    assert setup.handle_request({"operation": "validate_root", "path": path}) == {
        "ok": True, "path": path, "exists": False, "existing_parent": str(fake_home), "writable": True}
    assert not (fake_home / "future").exists()
    assert setup.handle_request({"operation": "validate_root", "path": str(fake_home)}) == {
        "ok": True, "path": str(fake_home), "exists": True, "existing_parent": str(fake_home), "writable": True}
    maximum = str(fake_home) + "/" + "x" * (240 - len(str(fake_home)) - 1)
    assert len(maximum) == 240 and setup.handle_request({"operation": "validate_root", "path": maximum})["ok"]


def test_work_root_canonicalizes_existing_home_alias_but_rejects_dangling_symlink(fake_home, tmp_path):
    alias = tmp_path / "home-link"
    alias.symlink_to(fake_home, target_is_directory=True)
    result = setup.handle_request({"operation": "validate_root", "path": str(alias / "future" / "jobs")})
    assert result["ok"] and result["path"] == str(fake_home / "future" / "jobs")
    (fake_home / "dangling").symlink_to(fake_home / "missing")
    assert setup.handle_request({"operation": "validate_root", "path": str(fake_home / "dangling" / "jobs")}) == {
        "ok": False, "error_code": "DANGLING_SYMLINK"}


@pytest.mark.parametrize("path", ["/", "relative", "/tmp/../jobs", "/tmp/.hidden/./jobs", "//tmp/jobs", "/tmp/jobs/",
                                 "/tmp/space here", "/tmp/quote'", "/tmp/$HOME", "/tmp/\u00fc", "/" + "x" * 240])
def test_work_root_requires_restricted_canonical_nonroot_path(path):
    assert setup.handle_request({"operation": "validate_root", "path": path}) == {"ok": False, "error_code": "INVALID_ROOT"}


def test_canonical_work_root_is_checked_again_after_symlink_resolution(fake_home, tmp_path):
    target = tmp_path / "actual root with spaces"
    target.mkdir()
    (fake_home / "safe-looking").symlink_to(target, target_is_directory=True)
    assert setup.handle_request({"operation": "validate_root", "path": str(fake_home / "safe-looking" / "jobs")}) == {
        "ok": False, "error_code": "INVALID_ROOT"}


def test_work_root_denies_non_directory_or_nonwritable_parent_without_private_diagnostics(fake_home, monkeypatch):
    (fake_home / "regular").write_text("private contents")
    assert setup.handle_request({"operation": "validate_root", "path": str(fake_home / "regular" / "jobs")}) == {
        "ok": False, "error_code": "NOT_A_DIRECTORY"}
    monkeypatch.setattr(setup.os, "access", lambda *_args, **_kwargs: False)
    assert setup.handle_request({"operation": "validate_root", "path": str(fake_home / "future")}) == {
        "ok": False, "error_code": "ROOT_NOT_WRITABLE"}


def test_metadata_operations_never_open_file_contents_or_write(fake_home, monkeypatch):
    (fake_home / "directory").mkdir()
    (fake_home / "private-file").write_text("secret")
    original_open, original_scandir = setup.os.open, setup.os.scandir
    scans = []

    def directories_only(path, flags, *args, **kwargs):
        assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
        return original_open(path, flags, *args, **kwargs)

    def one_level(fd):
        assert type(fd) is int
        scans.append(fd)
        return original_scandir(fd)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("content read, recursive scan or mutation attempted")

    monkeypatch.setattr(setup.os, "open", directories_only)
    monkeypatch.setattr(setup.os, "scandir", one_level)
    for name in ("read", "write", "walk", "listdir", "mkdir", "makedirs", "unlink", "rmdir"):
        monkeypatch.setattr(setup.os, name, forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    assert setup.handle_request({"operation": "identity"})["ok"]
    assert setup.handle_request({"operation": "browse"})["ok"]
    assert setup.handle_request({"operation": "validate_root", "path": str(fake_home / "future")})["ok"]
    assert len(scans) == 1


def test_canonical_directory_handles_refuse_symlink_substitution(fake_home, tmp_path, monkeypatch):
    target, replacement = fake_home / "target", fake_home / "replacement"
    target.mkdir()
    replacement.mkdir()
    original = setup._canonical_existing

    def swapped(path):
        canonical = original(path)
        if path == str(target):
            target.rmdir()
            target.symlink_to(replacement, target_is_directory=True)
        return canonical

    monkeypatch.setattr(setup, "_canonical_existing", swapped)
    result = setup.handle_request({"operation": "browse", "path": str(target)})
    assert result["ok"] is False and set(result) == {"ok", "error_code"}


def test_standalone_protocol_is_bounded_and_does_not_import_the_broker(fake_home):
    source = Path(setup.__file__).read_text()
    prefix = "import pwd,types\npwd.getpwuid=lambda _uid:types.SimpleNamespace(pw_dir=" + repr(str(fake_home)) + ")\n"
    for payload, code in [(b'{"operation":"browse","path":""}', None),
                          (b'{"operation":"browse","path":"","path":"/elsewhere"}', "INVALID_REQUEST"),
                          (b'{"operation":NaN}', "INVALID_REQUEST"), (b'[]', "INVALID_REQUEST"),
                          (b'x' * (setup.MAX_REQUEST_BYTES + 1), "REQUEST_TOO_LARGE")]:
        process = subprocess.run([sys.executable, "-I", "-c", prefix + "exec(" + repr(source) + ")"], input=payload,
                                 capture_output=True, cwd=fake_home, timeout=3)
        assert process.returncode == 0 and process.stderr == b""
        assert len(process.stdout) <= setup.MAX_RESPONSE_BYTES
        response = json.loads(process.stdout)
        if code:
            assert response == {"ok": False, "error_code": code}
        else:
            assert response["ok"] and response["path"] == str(fake_home)


@pytest.mark.parametrize("document", [{}, {"operation": "shell", "command": "pwd"}, {"operation": "identity", "path": "/"},
                                     {"operation": "validate_root"}, {"operation": "browse", "extra": "field"}])
def test_request_schema_is_closed(document):
    assert setup.handle_request(document) == {"ok": False, "error_code": "INVALID_REQUEST"}
