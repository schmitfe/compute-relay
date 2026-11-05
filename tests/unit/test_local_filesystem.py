"""macOS system aliases are canonicalized; selected symlinks remain forbidden."""

from pathlib import Path
import sys

import pytest

from compute_relay import local_filesystem


@pytest.mark.parametrize("path,expected", [("/tmp/work", "/private/tmp/work"),
    ("/var/folders/work", "/private/var/folders/work"), ("/etc/ssh/known_hosts", "/private/etc/ssh/known_hosts"),
    ("/Users/researcher/work", "/Users/researcher/work")])
def test_macos_canonicalizes_only_system_prefixes(monkeypatch, path, expected):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert local_filesystem.absolute_path(path) == Path(expected)


def test_macos_still_rejects_a_selected_symlink(monkeypatch, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(sys, "platform", "darwin")
    # The fixture path itself is already canonical on a Mac.
    with pytest.raises(ValueError, match="Symbolic links"):
        local_filesystem.check_path(link, directory=True)
