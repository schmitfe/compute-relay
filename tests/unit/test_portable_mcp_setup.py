"""Client portability preserves credentials, project guidance and path boundaries."""

import argparse
import hashlib
import json
from pathlib import Path
import stat
import shlex
import subprocess
import sys
import tomllib

import pytest

from compute_broker.mcp import setup, workflow


TOKEN = "cbp_synthetic_portable_project_credential_123456789"
# Frozen released bytes from 9be0c5a; migration evidence, not a maintained skill.
LEGACY_SKILL = Path(__file__).resolve().parents[1] / "fixtures/mcp/compute-skill-v1.md"


def test_both_clients_share_one_private_key_and_identical_packaged_skill(tmp_path):
    project = setup.initialize(tmp_path / "science", api_key=TOKEN, client="both", with_workflow=True,
                               broker_url="https://broker.example")
    codex = tomllib.loads((project / setup.CONFIG_PATH).read_text())["mcp_servers"]["compute_broker"]
    claude = json.loads((project / setup.CLAUDE_CONFIG_PATH).read_text())["mcpServers"]["compute_broker"]
    assert claude == {"type": "stdio", **codex}
    assert codex["env"] == {"BROKER_CONTROL_URL": "https://broker.example",
                            "BROKER_PROJECT_TOKEN_FILE": str(project / setup.KEY_PATH)}
    assert list((project / ".compute-broker").glob("*.key")) == [project / setup.KEY_PATH]
    assert (project / setup.KEY_PATH).read_text() == TOKEN + "\n"
    assert stat.S_IMODE((project / setup.KEY_PATH).stat().st_mode) == 0o600
    assert json.loads((project / setup.CONNECTION_PATH).read_text()) == {
        "schema_version": "1.0", "broker_url": "https://broker.example", "workspace_root": str(project)}
    for skill, instructions in workflow.CLIENTS.values():
        assert (project / skill).read_bytes() == workflow.skill_bytes()
        assert workflow.BLOCK_START in (project / instructions).read_text()
    for relative in (setup.CONFIG_PATH, setup.CLAUDE_CONFIG_PATH, setup.CONNECTION_PATH,
                     workflow.USAGE_PATH, Path("AGENTS.md"), Path("CLAUDE.md")):
        assert TOKEN not in (project / relative).read_text()
    assert stat.S_IMODE((project / workflow.USAGE_PATH).stat().st_mode) == 0o600


def test_claude_only_configuration_is_project_scoped_and_workflow_is_opt_in(tmp_path):
    setup.initialize(tmp_path, api_key=TOKEN, client="claude")
    assert not (tmp_path / ".codex").exists() and not (tmp_path / ".agents").exists()
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / "AGENTS.md").exists() and not (tmp_path / "CLAUDE.md").exists()
    server = json.loads((tmp_path / setup.CLAUDE_CONFIG_PATH).read_text())["mcpServers"]["compute_broker"]
    assert server["command"] == sys.executable and server["type"] == "stdio"
    assert server["args"] == ["-m", "compute_broker.mcp"]
    assert "/.mcp.json" in (tmp_path / ".gitignore").read_text().splitlines()


def test_claude_configuration_preserves_unrelated_servers_and_settings_atomically(tmp_path):
    before = {"mcpServers": {"other": {"type": "stdio", "command": "another-server", "env": {"CUSTOM": "keep"}}},
              "customSetting": {"nested": ["preserved", 1]}}
    (tmp_path / setup.CLAUDE_CONFIG_PATH).write_text(json.dumps(before))
    setup.initialize(tmp_path, api_key=TOKEN, client="claude")
    after = json.loads((tmp_path / setup.CLAUDE_CONFIG_PATH).read_text())
    assert after["mcpServers"]["other"] == before["mcpServers"]["other"]
    assert after["customSetting"] == before["customSetting"]
    assert not list(tmp_path.glob(".compute-broker-init-*"))


def test_add_second_client_reuses_key_only_with_explicit_option_and_keeps_first_config(tmp_path, monkeypatch):
    setup.initialize(tmp_path, api_key=TOKEN)
    original = (tmp_path / setup.CONFIG_PATH).read_bytes()
    key_info = (tmp_path / setup.KEY_PATH).stat()
    monkeypatch.setattr(setup.getpass, "getpass", lambda _prompt: pytest.fail("Existing key must not be prompted for"))
    with pytest.raises(ValueError, match="already has a broker API key"):
        setup.initialize(tmp_path, client="claude")
    setup.initialize(tmp_path, client="claude", reuse_key=True, with_workflow=True)
    assert (tmp_path / setup.CONFIG_PATH).read_bytes() == original
    assert (tmp_path / setup.KEY_PATH).stat().st_ino == key_info.st_ino
    assert not (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / ".claude/skills/compute/SKILL.md").read_bytes() == workflow.skill_bytes()


def test_reuse_can_add_workflow_to_existing_client_and_is_idempotent(tmp_path, monkeypatch):
    setup.initialize(tmp_path, api_key=TOKEN, client="both")
    setup.initialize(tmp_path, client="both", reuse_key=True, with_workflow=True)
    monkeypatch.setattr(setup, "_write", lambda *args: pytest.fail("Identical reusable setup must not rewrite files"))
    setup.initialize(tmp_path, client="both", reuse_key=True, with_workflow=True)


def test_reuse_requires_an_existing_private_key_and_forbids_imports(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.getpass, "getpass", lambda _prompt: pytest.fail("Reuse must not prompt"))
    with pytest.raises(ValueError, match="no existing private API key"):
        setup.initialize(tmp_path / "missing", reuse_key=True)
    assert not (tmp_path / "missing").exists()
    setup.initialize(tmp_path, api_key=TOKEN)
    with pytest.raises(ValueError, match="without importing"):
        setup.initialize(tmp_path, client="claude", reuse_key=True, api_key=TOKEN)
    (tmp_path / setup.KEY_PATH).chmod(0o644)
    with pytest.raises(ValueError, match="group/other"):
        setup.initialize(tmp_path, client="claude", reuse_key=True)
    assert not (tmp_path / setup.CLAUDE_CONFIG_PATH).exists()


def test_legacy_project_cannot_reuse_its_key_with_an_unselected_clients_different_origin(tmp_path):
    setup.initialize(tmp_path, api_key=TOKEN, broker_url="https://original.example")
    (tmp_path / setup.CONNECTION_PATH).unlink()  # Existing projects can predate workspace metadata.
    with pytest.raises(ValueError, match="different broker origin"):
        setup.initialize(tmp_path, client="claude", reuse_key=True, broker_url="https://other.example")
    assert not (tmp_path / setup.CLAUDE_CONFIG_PATH).exists()
    setup.initialize(tmp_path, client="claude", reuse_key=True, broker_url="https://original.example")


@pytest.mark.parametrize("content", ["invalid " + TOKEN, "[]", '{"mcpServers":2}',
    '{"mcpServers":{},"mcpServers":{}}', '{"mcpServers":{"compute_broker":{"command":"keep"}}}'])
def test_conflicting_claude_configuration_prevents_all_mutations_before_prompt(tmp_path, monkeypatch, content):
    (tmp_path / setup.CLAUDE_CONFIG_PATH).write_text(content)
    monkeypatch.setattr(setup.getpass, "getpass", lambda _prompt: pytest.fail("Must preflight before prompting"))
    with pytest.raises(ValueError) as caught:
        setup.initialize(tmp_path, client="both", with_workflow=True)
    assert TOKEN not in str(caught.value)
    assert (tmp_path / setup.CLAUDE_CONFIG_PATH).read_text() == content
    assert not (tmp_path / ".codex").exists() and not (tmp_path / ".compute-broker").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_instruction_append_preserves_tracked_project_rules_and_avoids_duplicate_blocks(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in ("AGENTS.md", "CLAUDE.md"):
        (tmp_path / name).write_text("# Science project\nPreserve our analysis conventions.\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "--", name], check=True)
    setup.initialize(tmp_path, api_key=TOKEN, client="both", with_workflow=True)
    setup.initialize(tmp_path, client="both", with_workflow=True, reuse_key=True)
    for name in ("AGENTS.md", "CLAUDE.md"):
        content = (tmp_path / name).read_text()
        assert content.startswith("# Science project\nPreserve our analysis conventions.\n")
        assert content.count(workflow.BLOCK_START) == content.count(workflow.BLOCK_END) == 1
        assert TOKEN not in content


def test_existing_different_skill_or_managed_block_is_never_overwritten(tmp_path, monkeypatch):
    skill = tmp_path / workflow.CLIENTS["claude"][0]
    skill.parent.mkdir(parents=True)
    skill.write_text("A different project compute skill")
    monkeypatch.setattr(setup.getpass, "getpass", lambda _prompt: pytest.fail("Must refuse before prompting"))
    with pytest.raises(ValueError, match="never overwrites a skill"):
        setup.initialize(tmp_path, client="both", with_workflow=True)
    assert not (tmp_path / ".codex").exists()
    skill.unlink()
    instructions = tmp_path / "CLAUDE.md"
    instructions.write_text(workflow.BLOCK_START + "\nProject-customized instructions\n" + workflow.BLOCK_END)
    with pytest.raises(ValueError, match="managed compute workflow instructions differ"):
        setup.initialize(tmp_path, client="both", with_workflow=True)
    assert not (tmp_path / ".compute-broker").exists()


@pytest.mark.parametrize("relative", [".mcp.json", ".claude", ".agents", "AGENTS.md", "CLAUDE.md", ".compute-broker/connection.json", ".compute-broker/USAGE.md"])
def test_new_setup_paths_reject_symlinks_before_prompting(tmp_path, monkeypatch, relative):
    project = tmp_path / "project"
    project.mkdir()
    path = project / relative
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    if relative in {".claude", ".agents"}:
        outside.mkdir()
    else:
        outside.write_text("keep")
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    monkeypatch.setattr(setup.getpass, "getpass", lambda _prompt: pytest.fail("Must check paths before prompting"))
    with pytest.raises(ValueError, match="Symbolic links"):
        setup.initialize(project, client="both", with_workflow=True)
    assert path.is_symlink() and not (project / setup.KEY_PATH).exists()
    if outside.is_file():
        assert outside.read_text() == "keep"


@pytest.mark.parametrize("relative", [".mcp.json", ".agents/skills/compute/SKILL.md", ".claude/skills/compute/SKILL.md"])
def test_tracked_deleted_private_or_skill_destinations_are_refused_before_prompt(tmp_path, monkeypatch, relative):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / relative
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text("tracked old file")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", relative], check=True)
    path.unlink()
    monkeypatch.setattr(setup.getpass, "getpass", lambda _prompt: pytest.fail("Must check Git before prompting"))
    with pytest.raises(ValueError, match="Git already tracks"):
        setup.initialize(tmp_path, client="both", with_workflow=True)
    assert not (tmp_path / setup.KEY_PATH).exists() and not path.exists()


def test_git_exclusions_cover_both_clients_even_after_prior_negations(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("/.mcp.json\n!/.mcp.json\n")
    setup.initialize(tmp_path, api_key=TOKEN, client="both")
    for relative in (setup.CONFIG_PATH, setup.CLAUDE_CONFIG_PATH, setup.KEY_PATH, setup.CONNECTION_PATH, workflow.USAGE_PATH):
        assert subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "--", str(relative)], capture_output=True).returncode == 0


def test_second_client_write_failure_rolls_back_other_config_and_guidance(tmp_path, monkeypatch):
    (tmp_path / ".codex").mkdir()
    codex = b'[mcp_servers.other]\ncommand = "keep"\n'
    (tmp_path / setup.CONFIG_PATH).write_bytes(codex)
    claude = b'{"mcpServers":{"other":{"command":"keep"}}}'
    (tmp_path / setup.CLAUDE_CONFIG_PATH).write_bytes(claude)
    ignore = b"results/\n"
    (tmp_path / ".gitignore").write_bytes(ignore)
    original_write = setup._write
    def fail_claude(path, *args):
        if path == tmp_path / setup.CLAUDE_CONFIG_PATH:
            raise OSError("Synthetic write failure")
        return original_write(path, *args)
    monkeypatch.setattr(setup, "_write", fail_claude)
    with pytest.raises(OSError):
        setup.initialize(tmp_path, api_key=TOKEN, client="both", with_workflow=True)
    assert (tmp_path / setup.CONFIG_PATH).read_bytes() == codex
    assert (tmp_path / setup.CLAUDE_CONFIG_PATH).read_bytes() == claude
    assert (tmp_path / ".gitignore").read_bytes() == ignore
    assert not (tmp_path / ".compute-broker").exists() and not (tmp_path / ".agents").exists()
    assert not list(tmp_path.glob(".compute-broker-init-*"))


def test_cli_exposes_portable_options_without_auto_approval_or_raw_key_arguments():
    parser = argparse.ArgumentParser()
    setup.register_parser(parser.add_subparsers())
    args = parser.parse_args(["init-mcp", "--client", "both", "--with-workflow", "--reuse-key"])
    assert args.client == "both" and args.with_workflow and args.reuse_key
    for option in ("--api-key", "--auto-approve", "--skip-permissions"):
        with pytest.raises(SystemExit):
            parser.parse_args(["init-mcp", option, TOKEN])


@pytest.mark.parametrize("client", ["codex", "claude", "both"])
def test_usage_guide_records_shell_quoted_module_commands_without_assuming_console_scripts(tmp_path, client):
    project = tmp_path / "science project 'quoted'"
    interpreter = tmp_path / "broker environment 'quoted'" / "bin/python"
    setup.initialize(project, api_key=TOKEN, client=client, with_workflow=True, python=interpreter)
    guide = (project / workflow.USAGE_PATH).read_text()
    commands = [line for block in guide.split("```sh\n")[1:] for line in block.split("\n```", 1)[0].splitlines()]
    assert commands
    actions = set()
    for command in commands:
        words = shlex.split(command)
        assert words[:4] == [str(interpreter), "-m", "compute_broker.cli", "files"]
        assert not words[0].endswith("compute-broker")
        if "--directory" in words:
            assert words[words.index("--directory") + 1] == str(project)
        actions.add(words[4])
    assert {"--help", "stage", "prepare", "fetch"} <= actions
    assert TOKEN not in guide and "BROKER_PROJECT_TOKEN=" not in guide
    selected = ("codex", "claude") if client == "both" else (client,)
    for name in selected:
        skill_path, instructions_path = workflow.CLIENTS[name]
        assert ".compute-broker/USAGE.md" in (project / skill_path).read_text()
        assert ".compute-broker/USAGE.md" in (project / instructions_path).read_text()
    assert json.loads((project / setup.CONNECTION_PATH).read_text()) == {
        "schema_version": "1.0", "broker_url": setup.DEFAULT_BROKER_URL, "workspace_root": str(project)}


def old_workflow_project(project):
    setup.initialize(project, api_key=TOKEN, client="both", with_workflow=True)
    old = LEGACY_SKILL.read_bytes()
    assert hashlib.sha256(old).hexdigest() in workflow.LEGACY_SKILL_SHA256
    for name, (skill_path, instructions_path) in workflow.CLIENTS.items():
        (project / skill_path).write_bytes(old)
        (project / instructions_path).write_text("# Existing project conventions\nKeep our workflow.\n\n"
            + workflow.legacy_instruction_block(name) + "\n## Local notes\nPreserve these too.\n")
    (project / workflow.USAGE_PATH).unlink()
    return project


def test_explicit_reuse_upgrades_exact_old_generated_skills_and_managed_blocks_idempotently(tmp_path, monkeypatch):
    project = old_workflow_project(tmp_path / "science")
    key_before = (project / setup.KEY_PATH).read_bytes()
    config_before = {path: (project / path).read_bytes() for path in (setup.CONFIG_PATH, setup.CLAUDE_CONFIG_PATH, setup.CONNECTION_PATH)}
    monkeypatch.setattr(setup.getpass, "getpass", lambda _prompt: pytest.fail("Upgrade must reuse the saved key"))
    setup.initialize(project, client="both", with_workflow=True, reuse_key=True)
    for skill_path, instructions_path in workflow.CLIENTS.values():
        assert (project / skill_path).read_bytes() == workflow.skill_bytes()
        instructions = (project / instructions_path).read_text()
        assert instructions.startswith("# Existing project conventions\nKeep our workflow.\n\n")
        assert instructions.endswith("\n## Local notes\nPreserve these too.\n")
        assert ".compute-broker/USAGE.md" in instructions
        assert instructions.count(workflow.BLOCK_START) == 1
    assert (project / workflow.USAGE_PATH).is_file()
    assert (project / setup.KEY_PATH).read_bytes() == key_before
    assert all((project / path).read_bytes() == before for path, before in config_before.items())
    monkeypatch.setattr(setup, "_write", lambda *args: pytest.fail("Identical upgrade must not rewrite files"))
    setup.initialize(project, client="both", with_workflow=True, reuse_key=True)


@pytest.mark.parametrize("conflict", ["guide", "skill", "instructions"])
def test_customized_guide_or_old_workflow_is_refused_without_partial_upgrade(tmp_path, monkeypatch, conflict):
    project = old_workflow_project(tmp_path / "science")
    if conflict == "guide":
        (project / workflow.USAGE_PATH).write_text("My own helper instructions")
    elif conflict == "skill":
        with (project / workflow.CLIENTS["claude"][0]).open("ab") as stream:
            stream.write(b"\nUser-specific changes\n")
    else:
        instructions = project / "CLAUDE.md"
        instructions.write_text(instructions.read_text().replace("Keep API keys", "User-specific rule: keep API keys"))
    before = {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}
    monkeypatch.setattr(setup.getpass, "getpass", lambda _prompt: pytest.fail("No key prompt during conflict preflight"))
    with pytest.raises(ValueError):
        setup.initialize(project, client="both", with_workflow=True, reuse_key=True)
    assert {path: path.read_bytes() for path in project.rglob("*") if path.is_file()} == before


def test_failed_old_workflow_upgrade_rolls_back_guidance_and_removes_only_new_guide(tmp_path, monkeypatch):
    project = old_workflow_project(tmp_path / "science")
    before = {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}
    original_write = setup._write
    def fail_last_instructions(path, *args):
        if path == project / "CLAUDE.md":
            raise OSError("Synthetic final guidance failure")
        return original_write(path, *args)
    monkeypatch.setattr(setup, "_write", fail_last_instructions)
    with pytest.raises(OSError):
        setup.initialize(project, client="both", with_workflow=True, reuse_key=True)
    assert {path: path.read_bytes() for path in project.rglob("*") if path.is_file()} == before
    assert not (project / workflow.USAGE_PATH).exists()


def test_existing_user_guide_is_preserved_before_prompt_or_mutation(tmp_path, monkeypatch):
    private = tmp_path / ".compute-broker"
    private.mkdir(mode=0o700)
    guide = tmp_path / workflow.USAGE_PATH
    guide.write_text("Human-authored guide, keep unchanged")
    monkeypatch.setattr(setup.getpass, "getpass", lambda _prompt: pytest.fail("Guide conflict must precede key prompt"))
    with pytest.raises(ValueError, match="never overwrites a user guide"):
        setup.initialize(tmp_path, client="both", with_workflow=True)
    assert guide.read_text() == "Human-authored guide, keep unchanged"
    assert not (tmp_path / setup.KEY_PATH).exists() and not (tmp_path / ".gitignore").exists()
