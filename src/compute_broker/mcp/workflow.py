"""One provider-neutral compute workflow, copied into client discovery locations."""

from importlib.resources import files
from pathlib import Path


CLIENTS = {
    "codex": (Path(".agents/skills/compute/SKILL.md"), Path("AGENTS.md")),
    "claude": (Path(".claude/skills/compute/SKILL.md"), Path("CLAUDE.md")),
}
BLOCK_START = "<!-- compute-broker:workflow:start -->"
BLOCK_END = "<!-- compute-broker:workflow:end -->"


def skill_bytes():
    return files("compute_broker.mcp").joinpath("resources", "SKILL.md").read_bytes()


def instruction_block(client):
    skill_path, _ = CLIENTS[client]
    return (f"{BLOCK_START}\n"
        "For scientific work that may use local or cluster compute, read the compute workflow "
        f"in `{skill_path.as_posix()}` and inspect the `compute_broker` project context before choosing placement. "
        "Keep API keys, SSH identities and MFA out of conversations. The workflow guides planning; "
        "the broker's current policy and approval requirements remain authoritative.\n"
        f"{BLOCK_END}\n")
