"""Local temporary directory metadata only; no SSH or real account home scan."""

import builtins
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from compute_relay.remote import connector_setup as setup


def test_storage_selection_creates_missing_parents_and_preserves_existing_permissions(tmp_path):
    existing = tmp_path / "shared"
    existing.mkdir(mode=0o750)
    result = setup.handle_request({"operation": "mkdir", "path": str(existing / "project" / "runs"),
        "name": "results", "parents": True})
    assert result["ok"] is True and result["created"] is True
    assert result["path"] == str(existing / "project" / "runs" / "results")
    assert stat.S_IMODE(existing.stat().st_mode) == 0o750
    for path in (existing / "project", existing / "project" / "runs", Path(result["path"])):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    again = setup.handle_request({"operation": "mkdir", "path": str(existing / "project" / "runs"),
        "name": "results", "parents": True})
    assert again["ok"] is True and again["created"] is False


def test_recursive_creation_resolves_existing_parent_alias_and_rejects_leaf_symlinks(fake_home, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(fake_home, target_is_directory=True)
    result = setup.handle_request({"operation": "mkdir", "path": str(alias / "new" / "runs"),
        "name": "results", "parents": True})
    assert result["ok"] and result["path"] == str(fake_home / "new" / "runs" / "results")
    (fake_home / "link").symlink_to(fake_home / "new", target_is_directory=True)
    denied = setup.handle_request({"operation": "mkdir", "path": str(fake_home), "name": "link", "parents": True})
    assert denied == {"ok": False, "error_code": "DIRECTORY_CONFLICT"}


def test_parent_creation_refuses_a_symlink_inserted_during_creation(fake_home, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    original = setup.os.mkdir
    def replaced(name, *args, **kwargs):
        if name == "new":
            os.symlink(outside, name, dir_fd=kwargs['dir_fd'])
            raise FileExistsError
        return original(name, *args, **kwargs)
    monkeypatch.setattr(setup.os, 'mkdir', replaced)
    result = setup.handle_request({"operation": "mkdir", "path": str(fake_home / "new" / "runs"),
        "name": "results", "parents": True})
    assert result['ok'] is False and not list(outside.iterdir())


def test_recursive_creation_validates_the_complete_path_before_writing(fake_home):
    missing = fake_home / ('a' * 75) / ('b' * 75)
    result = setup.handle_request({'operation': 'mkdir', 'path': str(missing), 'name': 'c' * 80, 'parents': True})
    assert result['ok'] is False and not (fake_home / ('a' * 75)).exists()


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


@pytest.mark.parametrize("mask", [0o002, 0o077, 0o777])
def test_mkdir_creates_exactly_private_directory_restores_umask_and_retries_idempotently(fake_home, mask):
    previous = os.umask(mask)
    try:
        document = {"operation": "mkdir", "path": str(fake_home), "name": ".jobs"}
        result = setup.handle_request(document)
        assert result == {"ok": True, "path": str(fake_home / ".jobs"), "parent": str(fake_home), "home": str(fake_home),
            "entries": [], "writable": True, "truncated": False, "scanned_entries": 0, "created": True}
        assert stat.S_IMODE((fake_home / ".jobs").stat().st_mode) == 0o700
        assert os.umask(mask) == mask
        result = setup.handle_request(document)
        assert result["created"] is False and result["path"] == str(fake_home / ".jobs")
        assert stat.S_IMODE((fake_home / ".jobs").stat().st_mode) == 0o700
    finally:
        os.umask(previous)


def test_mkdir_opens_existing_ordinary_directory_without_changing_it(fake_home, monkeypatch):
    directory = fake_home / "existing"
    directory.mkdir(mode=0o755)
    (directory / "child").mkdir()
    before = directory.stat()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Existing object was mutated")

    for name in ("mkdir", "chmod", "fchmod", "unlink", "rmdir", "rename"):
        monkeypatch.setattr(setup.os, name, forbidden)
    result = setup.handle_request({"operation": "mkdir", "path": str(fake_home), "name": "existing"})
    assert result["ok"] and result["created"] is False
    assert result["entries"] == [{"name": "child", "path": str(directory / "child")}]
    assert (directory.stat().st_ino, directory.stat().st_mode) == (before.st_ino, before.st_mode)


@pytest.mark.parametrize("name", ["", ".", "..", "nested/jobs", "slash\\name", "a b", "a\n", "a\x00", "$(touch x)", "`id`",
                                  "quoted'", 'quoted"', "name;id", "*", "\u00fc", "x" * 81])
def test_mkdir_rejects_unsafe_or_multiple_name_components_without_mutation(fake_home, name):
    result = setup.handle_request({"operation": "mkdir", "path": str(fake_home), "name": name})
    assert result == {"ok": False, "error_code": "INVALID_DIRECTORY_NAME"}
    assert list(fake_home.iterdir()) == []


def test_mkdir_requires_complete_canonical_job_path_before_creating_anything(fake_home, tmp_path):
    spaced = tmp_path / "directory with spaces"
    spaced.mkdir()
    oversized = fake_home / ("x" * (240 - len(str(fake_home))))
    oversized.mkdir()
    for parent in (str(spaced), str(oversized), str(fake_home) + "/../home"):
        result = setup.handle_request({"operation": "mkdir", "path": parent, "name": "jobs"})
        assert result["ok"] is False and result["error_code"] in {"INVALID_ROOT", "INVALID_PATH"}
    assert not (spaced / "jobs").exists() and not (oversized / "jobs").exists()


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling"])
def test_mkdir_refuses_existing_file_or_symlink_without_overwriting(fake_home, kind):
    target = fake_home / "target"
    if kind == "file":
        target.write_text("keep this content")
    elif kind == "symlink":
        (fake_home / "ordinary").mkdir()
        target.symlink_to(fake_home / "ordinary", target_is_directory=True)
    else:
        target.symlink_to(fake_home / "missing")
    before = target.lstat()
    assert setup.handle_request({"operation": "mkdir", "path": str(fake_home), "name": "target"}) == {
        "ok": False, "error_code": "DIRECTORY_CONFLICT"}
    assert (target.lstat().st_ino, target.lstat().st_mode) == (before.st_ino, before.st_mode)
    if kind == "file":
        assert target.read_text() == "keep this content"


def test_mkdir_denies_nonwritable_parent_before_attempting_creation(fake_home, monkeypatch):
    monkeypatch.setattr(setup, "_writable", lambda _fd: False)
    monkeypatch.setattr(setup.os, "mkdir", lambda *_args, **_kwargs: pytest.fail("Readonly parent was mutated"))
    assert setup.handle_request({"operation": "mkdir", "path": str(fake_home), "name": "jobs"}) == {
        "ok": False, "error_code": "ROOT_NOT_WRITABLE"}
    assert not (fake_home / "jobs").exists()


def test_mkdir_parent_alias_is_explicitly_canonical_in_child_result(fake_home, tmp_path):
    alias = tmp_path / "home-alias"
    alias.symlink_to(fake_home, target_is_directory=True)
    result = setup.handle_request({"operation": "mkdir", "path": str(alias), "name": "jobs"})
    assert result["ok"] and result["created"] and result["path"] == str(fake_home / "jobs")


def test_mkdir_safe_file_exists_race_returns_existing_without_claiming_creation(fake_home, monkeypatch):
    original = setup.os.mkdir

    def concurrently_created(name, mode, *, dir_fd):
        original(name, mode=0o700, dir_fd=dir_fd)
        raise FileExistsError

    monkeypatch.setattr(setup.os, "mkdir", concurrently_created)
    result = setup.handle_request({"operation": "mkdir", "path": str(fake_home), "name": "jobs"})
    assert result["ok"] and result["created"] is False


def test_mkdir_rejects_child_inode_replacement_between_stat_and_open(fake_home, monkeypatch):
    directory, saved = fake_home / "jobs", fake_home / "saved"
    directory.mkdir()
    original = setup.os.open
    replaced = False

    def swap_on_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "jobs" and not flags & getattr(os, "O_PATH", 0) and not replaced:
            replaced = True
            directory.rename(saved)
            directory.mkdir()
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(setup.os, "open", swap_on_open)
    assert setup.handle_request({"operation": "mkdir", "path": str(fake_home), "name": "jobs"}) == {
        "ok": False, "error_code": "DIRECTORY_CHANGED"}
    assert replaced and directory.is_dir() and saved.is_dir()


def test_mkdir_rejects_replacement_during_listing_without_returning_stale_metadata(fake_home, monkeypatch):
    directory, saved = fake_home / "jobs", fake_home / "saved"
    directory.mkdir()
    (directory / "private-metadata").mkdir()
    original = setup._browse_open_directory

    def swap_after_listing(path, home, fd):
        result = original(path, home, fd)
        directory.rename(saved)
        directory.symlink_to(saved, target_is_directory=True)
        return result

    monkeypatch.setattr(setup, "_browse_open_directory", swap_after_listing)
    assert setup.handle_request({"operation": "mkdir", "path": str(fake_home), "name": "jobs"}) == {
        "ok": False, "error_code": "DIRECTORY_CHANGED"}


def test_mkdir_parent_replacement_never_redirects_creation_to_symlink_destination(fake_home, monkeypatch):
    parent, moved, outside = fake_home / "parent", fake_home / "moved", fake_home / "outside"
    parent.mkdir()
    outside.mkdir()
    original = setup.os.mkdir

    def swap_parent(name, mode, *, dir_fd):
        parent.rename(moved)
        parent.symlink_to(outside, target_is_directory=True)
        original(name, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(setup.os, "mkdir", swap_parent)
    assert setup.handle_request({"operation": "mkdir", "path": str(parent), "name": "jobs"}) == {
        "ok": False, "error_code": "DIRECTORY_CHANGED"}
    assert not (outside / "jobs").exists() and (moved / "jobs").is_dir()


def test_standalone_mkdir_protocol_creates_only_one_directory_and_returns_retry_flag(fake_home):
    source = Path(setup.__file__).read_text()
    prefix = "import pwd,types\npwd.getpwuid=lambda _uid:types.SimpleNamespace(pw_dir=" + repr(str(fake_home)) + ")\n"
    command = [sys.executable, "-I", "-c", prefix + "exec(" + repr(source) + ")"]
    payload = json.dumps({"operation": "mkdir", "path": str(fake_home), "name": "jobs"}).encode()
    for expected in (True, False):
        process = subprocess.run(command, input=payload, capture_output=True, cwd=fake_home, timeout=3)
        assert process.returncode == 0 and process.stderr == b""
        result = json.loads(process.stdout)
        assert result["ok"] and result["created"] is expected and result["path"] == str(fake_home / "jobs")
        assert len(process.stdout) <= setup.MAX_RESPONSE_BYTES
    assert [entry.name for entry in fake_home.iterdir()] == ["jobs"]
