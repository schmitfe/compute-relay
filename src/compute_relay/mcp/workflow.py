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
        "Record a bounded experiment plan, useful deliverable and stopping rule before substantive compute. "
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
        "Request execution through the project's MCP connection or Python SDK and follow its actual review "
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
        "\n## Finish an investigation and export its run manifest\n\n"
        "After closing the scientific scope, export retained evidence and locally verified results. "
        "Replace INVESTIGATION_UUID and choose a fresh output directory:\n\n```sh\n"
        + command("files", "finish", "--directory", root, "--investigation-id", "INVESTIGATION_UUID", "--output", "results/INVESTIGATION") + "\n```\n\n"
        "For an ungrouped job, use --job-id JOB_UUID instead. Add --resume to refresh an existing export; "
        "add --acknowledge only to explicitly acknowledge reconciled, locally verified results. "
        "--no-download exports broker metadata with local delivery unverified. The run-manifest.json "
        "keeps scientific assessment, execution, collection, verification and accounting distinct. "
        "No finish/export command starts compute.\n"
        "\n## Orchestrate locally with Python\n\n"
        "For a bounded, preplanned sequence, an agent can write one local script instead of "
        "returning each status response to the model. The SDK reuses the same HTTP client and "
        "broker policy as MCP. Save the following as `run_compute.py` beside `main.py` and "
        "`job.json`; run it with `" + shlex.join([str(interpreter), "run_compute.py"]) + "`. "
        "Use the recorded interpreter, even if the science project has its own environment.\n\n"
        "```python\n"
        "import time\n"
        "from compute_relay.sdk import ControlError, ProjectFacade, WorkspaceClient, finish\n\n"
        "workspace = WorkspaceClient(" + repr(root) + ")\n"
        "project = ProjectFacade.from_credential(workspace.client)\n"
        'bundle = workspace.stage(["main.py"])\n'
        'job = workspace.prepare(bundle["bundle_id"], "job.json")\n'
        'print({"job_id": job["id"], "review_url": job["review_url"]})\n'
        'project.request_execution(job["id"])\n'
        "deadline, revision = time.monotonic() + 600, None\n"
        "while time.monotonic() < deadline:\n"
        "    try:\n"
        '        job = project.get_job(job["id"], after_revision=revision, wait_seconds=20 if revision else 0)\n'
        "    except ControlError as error:\n"
        '        if error.broker_code != "WAIT_BUSY":\n'
        "            raise\n"
        "        time.sleep(1)\n"
        "        continue\n"
        '    revision = job["revision"]\n'
        '    if job["completion"]["operationally_reconciled"]:\n'
        '        print(finish(workspace, job_id=job["id"], output="results/" + job["id"], resume=True))\n'
        "        break\n"
        '    if job["next_action"]["action"] in {"await_human_review", "revise_request", "recover_outputs"}:\n'
        '        print({"job_id": job["id"], "next_action": job["next_action"], "review_url": job["review_url"]})\n'
        "        break\n"
        "else:\n"
        '    print({"job_id": job["id"], "status": "still_pending"})\n'
        "```\n\n"
        "The preparation key is derived from the unchanged request and staged hashes; the "
        "default execution key reuses the first execution request. Rerunning an unchanged "
        "script therefore resumes the same job rather than buying another execution. Human "
        "review and output-recovery cases return for attention. A timeout stops local waiting "
        "after about ten minutes; it does not cancel admitted work. HTTP, scheduler and "
        "collection latency still exist. Print concise receipts, never credentials or source bytes. "
        "Hash verification does not establish scientific correctness.\n"
    ).encode("utf-8")
