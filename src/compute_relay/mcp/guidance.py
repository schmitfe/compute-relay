"""Shared, on-demand guidance for resource-capable and tool-only MCP clients."""

from importlib.resources import files
import json

TOPICS = {"workflow": "SKILL.md", "placement": "PLACEMENT.md", "completion": "COMPLETION.md"}


def guide(topic):
    if topic == "job_request":
        from .requests import client_job_schema
        return {"topic": topic, "schema": client_job_schema(), "example": {
            "schema_version": "1.0", "connection_id": "11111111-1111-4111-8111-111111111111", "name": "bounded-pilot",
            "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
            "environment": {"id": "22222222-2222-4222-8222-222222222222", "revision_sha256": "e" * 64},
            "resources": {"nodes": 1, "cpus": 2, "gpus": 0, "memory_mib": 1024, "time_limit_seconds": 60},
            "slurm": {"partition": "debug", "account": "broker"},
            "files": [{"path": "main.py", "sha256": "b" * 64, "size_bytes": 42, "executable": False}], "outputs": ["result.json"]},
            "notes": "Replace the example identities and file descriptor with current context and staging receipts. Host memory_mib does not reserve GPU VRAM."}
    if topic not in TOPICS:
        raise ValueError("Unknown compute guide topic")
    return {"topic": topic, "resource_uri": "compute-relay://guide/" + topic,
        "text": files("compute_relay.mcp").joinpath("resources", TOPICS[topic]).read_text()}


def register_resources(server):
    @server.resource("compute-relay://guide/workflow", mime_type="text/markdown")
    def workflow_guide():
        """Scientific planning, bounded execution and stopping rules."""
        return guide("workflow")["text"]

    @server.resource("compute-relay://guide/placement", mime_type="text/markdown")
    def placement_guide():
        """Placement, scheduler pressure and environment observations."""
        return guide("placement")["text"]

    @server.resource("compute-relay://guide/completion", mime_type="text/markdown")
    def completion_guide():
        """Completion states, recovery and verified manifest export."""
        return guide("completion")["text"]

    @server.resource("compute-relay://guide/job_request", mime_type="application/json")
    def job_request_guide():
        """Typed resource schema and a small CPU request example."""
        return json.dumps(guide("job_request"))
