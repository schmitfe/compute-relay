"""Bounded human job navigation, shared by full pages and live fragments."""

from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q
from django.db.models.functions import Coalesce

from .models import Attempt, ExecutionRequest, Job
from .services import TERMINAL

FILTERS = [("all", "All jobs"), ("unresolved", "Unresolved executions"),
           ("awaiting_review", "Awaiting review")]


def job_list_context(request, projects, *, page_path=None):
    page_path = page_path or request.path
    selected = request.GET.get("job_state", "all")
    if selected not in dict(FILTERS):
        selected = "all"
    query = Job.objects.with_summary().filter(project__in=projects)
    counts = query.order_by().annotate(current_state=Coalesce("_latest_attempt_state", "_latest_execution_state", "state"))
    counts = [{"label": Job.state_label(row["current_state"]), "count": row["count"]}
        for row in counts.values("current_state").annotate(count=Count("id")).order_by("current_state")]
    if selected != "all":
        pending = ExecutionRequest.objects.filter(job_id=OuterRef("pk"), state="awaiting_approval")
        query = query.annotate(_pending_review=Exists(pending))
        if selected == "awaiting_review":
            query = query.filter(_pending_review=True)
        else:
            unresolved = Attempt.objects.filter(job_id=OuterRef("pk")).filter(
                ~Q(state__in=TERMINAL) | ~Q(accounting_state__in=["settled", "released"])
                | Q(collection_pending=True) | Q(collection_state="failed"))
            query = query.annotate(_unresolved=Exists(unresolved)).filter(
                Q(_pending_review=True) | Q(_unresolved=True))
    page = Paginator(query.order_by("-created_at", "-id"), 50).get_page(request.GET.get("page"))
    parameters = {"job_state": selected, "page": page.number}
    return {"jobs": page.object_list, "job_page": page, "job_state": selected, "job_counts": counts,
        "job_filters": FILTERS, "job_live_query": urlencode(parameters), "job_list_path": page_path,
        "job_previous_url": page_path + "?" + urlencode({**parameters, "page": page.previous_page_number()}) + "#jobs" if page.has_previous() else None,
        "job_next_url": page_path + "?" + urlencode({**parameters, "page": page.next_page_number()}) + "#jobs" if page.has_next() else None}
