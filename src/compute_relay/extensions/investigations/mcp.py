from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field
from mcp.types import ToolAnnotations


class Budget(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    cpu_core_seconds: Annotated[int, Field(ge=0, le=2**53 - 1, description="Total planned allocated CPU core-seconds, including validation and diagnostics.")]
    gpu_device_seconds: Annotated[int, Field(ge=0, le=2**53 - 1, description="Total planned GPU device-seconds; 0 for CPU-only work.")]
    max_executions: Annotated[int, Field(ge=1, le=10000, description="Maximum planned executions across all agents, retries and stages.")]


class InvestigationPlan(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    question: Annotated[str, Field(min_length=1, max_length=4000)]
    deliverable: Annotated[str, Field(min_length=1, max_length=4000, description="Concrete report, result table or other useful output.")]
    core_experiments: Annotated[list[Annotated[str, Field(min_length=1, max_length=512)]], Field(min_length=1, max_length=32)]
    required_validation: Annotated[list[Annotated[str, Field(min_length=1, max_length=512)]], Field(max_length=32)]
    stopping_rule: Annotated[str, Field(min_length=1, max_length=4000, description="Evidence sufficient to finish, plus when to report inconclusive and stop.")]
    budget: Budget
    diagnostic_limit: Annotated[int, Field(ge=0, le=32, description="Maximum additional diagnostic experiments; shared by every agent and handoff.")]


def register(server, facade):
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

    @server.tool(annotations=write)
    def create_investigation(name: str, plan: InvestigationPlan, idempotency_key: str | None = None) -> dict:
        """Record a finite scientific plan. Plan limits guide agents; the project grant enforces spending. Executes nothing."""
        return facade._mutate(f"/api/v1/projects/{facade.project_id}/investigations",
            {"name": name, "plan": plan.model_dump()}, idempotency_key)

    @server.tool(annotations=read)
    def list_investigations(state: Literal["open", "closed", "all"] = "open", cursor: int = 0, limit: int = 20) -> dict:
        """List this project's investigation identities and scope state; paginate with next_cursor."""
        return facade.list_investigations(state, cursor, limit)

    @server.tool(annotations=read)
    def get_investigation(investigation_id: str, after_revision: str | None = None, wait_seconds: int = 0) -> dict:
        """Read grouped jobs, abandoned drafts, aggregate accounting and closeout blockers. Wait at most 20 seconds for a revision change."""
        return facade.get_investigation(investigation_id, after_revision, wait_seconds)

    @server.tool(annotations=write)
    def add_investigation_jobs(investigation_id: str, job_ids: list[str], role: Literal["core", "validation", "exploration"] = "core", idempotency_key: str | None = None) -> dict:
        """Attach up to 32 existing project jobs. New jobs can instead use prepare_job.investigation_id; revisions inherit grouping."""
        return facade.add_investigation_jobs(investigation_id, job_ids, role, idempotency_key)

    @server.tool(annotations=write)
    def finish_investigation(investigation_id: str, conclusion: Literal["sufficient", "inconclusive", "user_stopped"], report: str,
                             cancel_unstarted: bool = False, idempotency_key: str | None = None) -> dict:
        """Close scientific scope and record a report. Optionally cancel drafts/pending reviews; admitted work and accounting remain visible. Export verified results with files finish."""
        return facade.finish_investigation(investigation_id, conclusion, report, cancel_unstarted, idempotency_key)
