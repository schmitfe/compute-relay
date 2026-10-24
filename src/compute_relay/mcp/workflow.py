"""One provider-neutral compute workflow, copied into client discovery locations."""

from importlib.resources import files
from pathlib import Path
import shlex


CLIENTS = {
    "codex": (Path(".agents/skills/compute/SKILL.md"), Path("AGENTS.md")),
    "claude": (Path(".claude/skills/compute/SKILL.md"), Path("CLAUDE.md")),
}
BLOCK_START = "<!-- compute-relay:workflow:start -->"
BLOCK_END = "<!-- compute-relay:workflow:end -->"
USAGE_PATH = Path(".compute-relay/USAGE.md")


def skill_bytes():
    return files("compute_relay.mcp").joinpath("resources", "SKILL.md").read_bytes()


def instruction_block(client):
    skill_path, _ = CLIENTS[client]
    return (f"{BLOCK_START}\n"
        "For scientific work that may use local or cluster compute, read the compute workflow "
        f"in `{skill_path.as_posix()}` and inspect the `compute_relay` project context before choosing placement. "
        f"Read [the local helper guide]({USAGE_PATH.as_posix()}) for the exact installed Python command; "
        "do not assume `compute-relay` is on PATH. "
        "Keep API keys, SSH identities and MFA out of conversations. The workflow guides planning; "
        "the broker's current policy and approval requirements remain authoritative.\n"
        f"{BLOCK_END}\n")


def usage_guide(project, interpreter):
    """Machine-local instructions with shell-quoted commands and no credentials."""
    def command(*arguments):
        return shlex.join([str(interpreter), "-m", "compute_relay.cli", *arguments])
    root = str(project)
    return (
        "# Compute Relay commands for this workspace\n\n"
        "This generated guide contains no API key or SSH credentials. Commands use the "
        "installed Python interpreter directly and work without activating an environment "
        "or finding a console script on PATH. Run them on the device holding this workspace.\n\n"
        "## Inspect helper options\n\n```sh\n" + command("files", "--help") + "\n```\n\n"
        "## Stage explicit source files\n\n"
        "Create `source-bundle.json` in the workspace with an explicit file list, for example "
        '`{"files":["main.py","src/util.py"]}`. Then run:\n\n```sh\n'
        + command("files", "stage", "--directory", root, "--manifest", "source-bundle.json") + "\n```\n\n"
        "Or supply relative source paths directly:\n\n```sh\n"
        + command("files", "stage", "--directory", root, "main.py", "src/util.py") + "\n```\n\n"
        "There is no default recursive scan. Staging prints a bundle ID and checksums, not file bytes.\n\n"
        "## Prepare a package without executing it\n\n"
        "Save `job.json` in the workspace, omitting `files` and `project_id`; the saved bundle "
        "and project key supply those fields. Replace `BUNDLE_ID` and `WORKLOAD_STAGE` below "
        "with the staged bundle ID and a stable idempotency key:\n\n```sh\n"
        + command("files", "prepare", "--directory", root, "--bundle", "BUNDLE_ID", "--request", "job.json", "--idempotency-key", "WORKLOAD_STAGE") + "\n```\n\n"
        "Request execution through the project's MCP connection and follow its actual review "
        "and budget requirements. This prepare command does not execute a job.\n\n"
        "## Fetch declared, retained results\n\n"
        "Replace `ATTEMPT_UUID` and choose a fresh output subdirectory:\n\n```sh\n"
        + command("files", "fetch", "--directory", root, "--attempt-id", "ATTEMPT_UUID", "--output", "results/RUN") + "\n```\n\n"
        "The helper verifies hashes and writes a receipt. Scientific validation and final "
        "accounting remain separate checks. Keep file bytes out of the conversation.\n\n"
        "Private `.compute-relay/connection.json` selects the broker and workspace; "
        "`.compute-relay/project.key` contains the project credential. Do not paste "
        "credential contents, SSH identities, passphrases or MFA codes into chat. "
        "Human-owned connectors perform cluster SSH access; these file commands do not log in to a cluster.\n"
    ).encode("utf-8")
