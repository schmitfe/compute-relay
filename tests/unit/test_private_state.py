"""Private controller state is created safely and never followed through links."""

import os

from django.core.exceptions import ImproperlyConfigured
import pytest

from compute_relay.control.private_state import private_directory, signing_secret


def test_new_state_is_private_and_secret_is_reused(tmp_path):
    root = private_directory(tmp_path / "private")
    assert root.stat().st_mode & 0o777 == 0o700
    secret = root / "signing-key"
    first = signing_secret(secret)
    assert first == signing_secret(secret) and len(first) >= 32
    assert secret.stat().st_mode & 0o777 == 0o600


def test_existing_public_state_is_rejected_without_chmod(tmp_path):
    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    with pytest.raises(ImproperlyConfigured, match="private"):
        private_directory(root)
    assert root.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("kind", ["symlink", "fifo", "public", "hardlink"])
def test_unsafe_signing_file_is_rejected_without_reading_contents(tmp_path, kind):
    target = tmp_path / "target"
    target.write_text("synthetic-private-value-with-enough-characters")
    target.chmod(0o600)
    secret = tmp_path / "secret"
    if kind == "symlink":
        secret.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(secret, 0o600)
    elif kind == "hardlink":
        os.link(target, secret)
    else:
        secret.write_text(target.read_text())
        secret.chmod(0o644)
    with pytest.raises(ImproperlyConfigured) as failure:
        signing_secret(secret)
    assert "synthetic-private-value" not in str(failure.value)
