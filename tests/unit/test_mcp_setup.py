"""Private project onboarding stays offline and never overwrites existing credentials."""

import argparse
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tomllib
import warnings

import pytest

from compute_relay.mcp import setup
from compute_relay.local_config import broker_url


TOKEN = "cbp_" + "synthetic_project_credential_" * 2


def test_initialization_creates_private_files_and_only_key_scoped_mcp_config(tmp_path):
    project = setup.initialize(tmp_path / "science", api_key=TOKEN)
    config = tomllib.loads((project / setup.CONFIG_PATH).read_text())["mcp_servers"]["compute_relay"]
    assert config == {"command": sys.executable, "args": ["-m", "compute_relay.mcp"],
                      "env": {"BROKER_CONTROL_URL": setup.DEFAULT_BROKER_URL,
                              "BROKER_PROJECT_TOKEN_FILE": str(project / setup.KEY_PATH)}}
    assert (project / setup.KEY_PATH).read_text() == TOKEN + "\n"
    assert TOKEN not in (project / setup.CONFIG_PATH).read_text()
    for path in (project, project / ".codex", project / ".compute-relay"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (project / setup.KEY_PATH, project / setup.CONFIG_PATH, project / ".gitignore"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert all(path in (project / ".gitignore").read_text().splitlines() for path in setup.IGNORE_PATHS)


def test_existing_toml_and_gitignore_are_preserved_and_exclusions_are_effective(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".codex").mkdir()
    config = b'[preferences]\ntheme = "dark"\n[mcp_servers.other]\ncommand = "other-command"'
    (tmp_path / setup.CONFIG_PATH).write_bytes(config)
    ignore = b"results/\n!/.compute-relay/project.key"
    (tmp_path / ".gitignore").write_bytes(ignore)
    setup.initialize(tmp_path, api_key=TOKEN, broker_url="https://broker.example/")
    content = (tmp_path / setup.CONFIG_PATH).read_bytes()
    assert content.startswith(config)
    assert tomllib.loads(content.decode())["mcp_servers"]["other"]["command"] == "other-command"
    assert (tmp_path / ".gitignore").read_bytes().startswith(ignore + b"\n")
    assert subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "--", str(setup.KEY_PATH), str(setup.CONFIG_PATH)],
                          capture_output=True).returncode == 0


def test_interactive_prompt_does_not_echo_or_print_the_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: TOKEN)
    parser = argparse.ArgumentParser()
    setup.register_parser(parser.add_subparsers())
    args = parser.parse_args(["init-mcp", "--directory", str(tmp_path)])
    args.run(args)
    output = capsys.readouterr().out
    assert "Start a new trusted Codex chat" in output
    assert TOKEN not in output
    assert "BROKER_PROJECT_ID" not in (tmp_path / setup.CONFIG_PATH).read_text()


def test_cli_has_no_raw_key_argument():
    parser = argparse.ArgumentParser()
    setup.register_parser(parser.add_subparsers())
    with pytest.raises(SystemExit):
        parser.parse_args(["init-mcp", "--api-key", TOKEN])


def test_top_level_cli_initializes_offline_using_a_private_import(tmp_path):
    source = tmp_path / "source.key"
    source.write_text(TOKEN + "\n")
    source.chmod(0o600)
    project = tmp_path / "project"
    result = subprocess.run([sys.executable, "-m", "compute_relay.cli", "init-mcp", "--directory", str(project),
                             "--key-file", str(source), "--broker-url", "https://offline.invalid"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert TOKEN not in result.stdout + result.stderr
    config = tomllib.loads((project / setup.CONFIG_PATH).read_text())["mcp_servers"]["compute_relay"]
    assert config["env"] == {"BROKER_CONTROL_URL": "https://offline.invalid",
                              "BROKER_PROJECT_TOKEN_FILE": str(project / setup.KEY_PATH)}
    assert (project / setup.KEY_PATH).read_text() == TOKEN + "\n"
    assert all(path in (project / ".gitignore").read_text().splitlines() for path in setup.IGNORE_PATHS)


def test_private_import_is_copied_without_changing_source(tmp_path, capsys):
    source = tmp_path / "source.key"
    source.write_text(TOKEN + "\n")
    source.chmod(0o600)
    project = setup.initialize(tmp_path / "project", key_file=source)
    assert (project / setup.KEY_PATH).read_bytes() == source.read_bytes()
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert TOKEN not in capsys.readouterr().out


@pytest.mark.parametrize("unsafe", ["permissions", "symlink", "symlink-parent", "hardlink", "directory"])
def test_import_refuses_unsafe_source(tmp_path, unsafe):
    source = tmp_path / "source.key"
    source.write_text(TOKEN)
    source.chmod(0o600)
    selected = source
    if unsafe == "permissions":
        source.chmod(0o644)
    elif unsafe == "symlink":
        selected = tmp_path / "linked.key"
        selected.symlink_to(source)
    elif unsafe == "symlink-parent":
        linked = tmp_path / "linked"
        linked.symlink_to(tmp_path, target_is_directory=True)
        selected = linked / source.name
    elif unsafe == "hardlink":
        selected = tmp_path / "hard.key"
        selected.hardlink_to(source)
    elif unsafe == "directory":
        selected = tmp_path
    project = tmp_path / "project"
    with pytest.raises(ValueError):
        setup.initialize(project, key_file=selected)
    assert not project.exists()


@pytest.mark.parametrize("relative", [".compute-relay", ".codex", ".gitignore", ".codex/config.toml", ".compute-relay/project.key"])
def test_destination_rejects_symlinks_and_preserves_targets(tmp_path, relative):
    project = tmp_path / "project"
    project.mkdir()
    path = project / relative
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    if relative in {".compute-relay", ".codex"}:
        outside.mkdir()
    else:
        outside.write_text("unchanged")
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    with pytest.raises(ValueError, match="Symbolic links"):
        setup.initialize(project, api_key=TOKEN)
    assert path.is_symlink()
    if outside.is_file():
        assert outside.read_text() == "unchanged"


def test_destination_rejects_ancestor_symlink(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(ValueError, match="Symbolic links"):
        setup.initialize(tmp_path / "linked/project", api_key=TOKEN)
    assert not (tmp_path / "real/project").exists()


def test_existing_key_is_never_overwritten_even_if_config_is_missing(tmp_path):
    (tmp_path / ".compute-relay").mkdir(mode=0o700)
    path = tmp_path / setup.KEY_PATH
    path.write_text("keep this key")
    with pytest.raises(ValueError, match="already has a broker API key"):
        setup.initialize(tmp_path, api_key=TOKEN)
    assert path.read_text() == "keep this key"
    assert not (tmp_path / ".gitignore").exists()


@pytest.mark.parametrize("content", ['[mcp_servers.compute_relay]\ncommand = "existing"',
                                           "invalid TOML = [", "mcp_servers = 2",
                                           "mcp_servers = {}"])
def test_conflicting_or_invalid_toml_is_refused_before_prompting(tmp_path, monkeypatch, content):
    (tmp_path / ".codex").mkdir()
    (tmp_path / setup.CONFIG_PATH).write_text(content)
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: pytest.fail("Must validate before prompting"))
    with pytest.raises(ValueError):
        setup.initialize(tmp_path)
    assert (tmp_path / setup.CONFIG_PATH).read_text() == content
    assert not (tmp_path / ".compute-relay").exists()


@pytest.mark.parametrize("url", ["http://example.org", "https://user:pass@example.org", "https://@example.org",
                               "https://example.org/path", "https://example.org/?key=secret", "file:///tmp",
                               "https://example.org:invalid", "https://example.org:0", " https://example.org", "https://example .org",
                               "http://127.0.0.2:8768"])
def test_invalid_origin_does_not_create_project(tmp_path, url):
    project = tmp_path / "project"
    with pytest.raises(ValueError):
        setup.initialize(project, api_key=TOKEN, broker_url=url)
    assert not project.exists()


@pytest.mark.parametrize("url", ["http://localhost:8768", "http://[::1]:8768", "http://127.0.0.1:8768", "https://broker.example"])
def test_loopback_http_and_https_origins_are_accepted(url):
    assert broker_url(url) == url


@pytest.mark.parametrize("relative", [".compute-relay/project.key", ".compute-relay/other-private-file", ".codex/config.toml"])
def test_git_tracked_paths_are_refused_including_deleted_credentials(tmp_path, relative):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    target = tmp_path / relative
    target.parent.mkdir(mode=0o700, parents=True)
    target.write_text("# tracked path")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", relative], check=True)
    target.unlink()
    with pytest.raises(ValueError, match="Git already tracks"):
        setup.initialize(tmp_path, api_key=TOKEN)
    assert not (tmp_path / ".gitignore").exists()
    assert not target.exists()


@pytest.mark.parametrize("name", ["missing-project", "science[1]", "science*", "science?"])
def test_removed_project_directory_with_tracked_key_is_refused_before_prompt(tmp_path, monkeypatch, name):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    project = tmp_path / name
    key = project / setup.KEY_PATH
    key.parent.mkdir(mode=0o700, parents=True)
    key.write_text("previously tracked private path")
    subprocess.run(["git", "--literal-pathspecs", "-C", str(tmp_path), "add", "--", str(key.relative_to(tmp_path))], check=True)
    shutil.rmtree(project)
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: pytest.fail("Must detect tracked path before prompting"))
    with pytest.raises(ValueError, match="Git already tracks"):
        setup.initialize(project)
    assert not project.exists()


def test_missing_project_in_repository_initializes_when_private_paths_are_untracked(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    project = setup.initialize(tmp_path / "missing/science[1]", api_key=TOKEN)
    assert (project / setup.KEY_PATH).read_text() == TOKEN + "\n"
    assert subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "--", str(project / setup.KEY_PATH)],
                          capture_output=True).returncode == 0


def test_write_failure_removes_only_new_files_and_keeps_existing_configuration(tmp_path, monkeypatch):
    (tmp_path / ".codex").mkdir()
    config_path = tmp_path / setup.CONFIG_PATH
    config_path.write_text('[preferences]\ntheme = "dark"\n')
    original = config_path.read_bytes()
    write = setup._write

    def fail_config(path, before, content, created):
        if path == config_path:
            raise OSError("Synthetic write failure")
        return write(path, before, content, created)

    monkeypatch.setattr(setup, "_write", fail_config)
    with pytest.raises(OSError):
        setup.initialize(tmp_path, api_key=TOKEN)
    assert config_path.read_bytes() == original
    assert (tmp_path / ".codex").is_dir()
    assert not (tmp_path / ".compute-relay").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_new_project_is_removed_on_write_failure(tmp_path, monkeypatch):
    project = tmp_path / "new/project"
    monkeypatch.setattr(setup, "_write", lambda *args: (_ for _ in ()).throw(OSError("Synthetic failure")))
    with pytest.raises(OSError):
        setup.initialize(project, api_key=TOKEN)
    assert not (tmp_path / "new").exists()


def test_invalid_key_error_never_includes_the_secret(tmp_path, capsys):
    source = tmp_path / "source.key"
    source.write_text("not-a-valid-api-key-private")
    source.chmod(0o600)
    with pytest.raises(SystemExit) as caught:
        setup.main(argparse.Namespace(directory=str(tmp_path / "project"), broker_url=setup.DEFAULT_BROKER_URL,
                                      key_file=str(source)))
    assert source.read_text() not in str(caught.value)
    assert source.read_text() not in capsys.readouterr().out
    assert not (tmp_path / "project").exists()


def test_initialization_without_git_works_outside_a_repository(tmp_path, monkeypatch):
    def absent_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(setup.subprocess, "run", absent_git)
    setup.initialize(tmp_path, api_key=TOKEN)
    assert (tmp_path / setup.KEY_PATH).read_text() == TOKEN + "\n"


def test_missing_git_inside_repository_refuses_before_credential_write(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("git")))
    with pytest.raises(ValueError, match="Install Git"):
        setup.initialize(tmp_path, api_key=TOKEN)
    assert not (tmp_path / setup.KEY_PATH).exists()


def test_unreadable_repository_is_not_treated_as_an_untracked_project(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 128, b""))
    with pytest.raises(ValueError, match="Git could not inspect"):
        setup.initialize(tmp_path, api_key=TOKEN)
    assert not (tmp_path / setup.KEY_PATH).exists()


def test_prompt_cancellation_exits_cleanly_before_creating_files(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(SystemExit, match="MCP setup cancelled"):
        setup.main(argparse.Namespace(directory=str(tmp_path / "project"), broker_url=setup.DEFAULT_BROKER_URL, key_file=None))
    assert not (tmp_path / "project").exists()


def test_prompt_refuses_getpass_fallback_that_would_echo_the_key(tmp_path, monkeypatch):
    def insecure_prompt(prompt):
        warnings.warn("Cannot hide terminal input", setup.getpass.GetPassWarning)
        pytest.fail("Prompt must fail before echoed input")

    monkeypatch.setattr(setup.getpass, "getpass", insecure_prompt)
    with pytest.raises(SystemExit, match="interactive terminal"):
        setup.main(argparse.Namespace(directory=str(tmp_path / "project"), broker_url=setup.DEFAULT_BROKER_URL, key_file=None))
    assert not (tmp_path / "project").exists()
