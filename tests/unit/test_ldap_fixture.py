"""The optional mock uses private persistent synthetic credentials, never exports."""

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def bootstrap():
    path = Path(__file__).resolve().parents[2] / "deploy" / "ldap" / "bootstrap.py"
    spec = importlib.util.spec_from_file_location("ldap_fixture_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_passwords_are_distinct_private_and_stable(tmp_path, monkeypatch):
    module = bootstrap();calls = []
    def fake_run(arguments, **options):
        assert arguments[:2] == ["slappasswd", "-T"]
        assert options == {"capture_output": True, "text": True, "check": True, "timeout": 5}
        calls.append(arguments)
        return SimpleNamespace(stdout="{SSHA}synthetic-hash\n")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    public, private = tmp_path / "public", tmp_path / "private"
    template = tmp_path / "seed";template.write_text("\n".join("@@" + name.upper() + "_PASSWORD@@" for name in module.ACCOUNTS))
    module.initialize(public, private, template, owner=os.getuid())
    passwords = {name: (public / (name + "-password")).read_bytes() for name in module.ACCOUNTS}
    assert len(set(passwords.values())) == 5 and all(len(value) > 32 for value in passwords.values())
    assert all((public / (name + "-password")).stat().st_mode & 0o777 == 0o600 for name in module.ACCOUNTS)
    seed = (private / "seed.generated.ldif").read_bytes()
    assert b"@@" not in seed and not any(value.rstrip() in seed for value in passwords.values())
    (private / "data").mkdir();(private / "data" / "data.mdb").touch()
    module.initialize(public, private, template, owner=os.getuid())
    assert len(calls) == 5
    assert passwords == {name: (public / (name + "-password")).read_bytes() for name in module.ACCOUNTS}


@pytest.mark.parametrize("case", ["symlink", "permissions", "hardlink"])
def test_fixture_rejects_unsafe_existing_password_files(tmp_path, case):
    module = bootstrap();public = tmp_path / "public";public.mkdir()
    path = public / "reader-password"
    other = tmp_path / "other";other.write_text("synthetic");other.chmod(0o600)
    if case == "symlink": path.symlink_to(other)
    elif case == "hardlink": os.link(other, path)
    else: path.write_text("synthetic");path.chmod(0o644)
    with pytest.raises(RuntimeError, match="Unsafe"):
        module.initialize(public, tmp_path / "private", other, owner=os.getuid())
