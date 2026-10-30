"""GET /api/search: the command bar's one search.

It answers with the reads the bar used to make itself: an exact case and alert
id, the case full-text and IOC searches, and the findings text search. It adds
workflows, agents and skills, matched by the findings search's rule: every
term must appear. A kind the caller's role may not read is left empty.
"""

import re
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from core.auth.current_user import get_current_user
from core.auth.permissions import missing_permission
from core.cases.case_search_service import CaseSearchService
from core.deps import provide_workflows
from core.routing import Auth, RouterMeta
from core.skills.skill_library import load_skills, skill_roots
from core.storage.database_data_service import DatabaseDataService
from core.storage.models import User
from core.storage.unit_of_work import unit_of_work
from core.workflows.workflows_service import WorkflowsService
from services.api.routers.agents import agent_manager

router = APIRouter()

ROUTER_META = RouterMeta(prefix="/api/search", tags=["search"], auth=Auth.REQUIRED)

# Below this a trigram index can't serve the match, and an id isn't this short:
# only the exact-id reads run.
MIN_SEARCH_CHARS = 3
# Hits per kind; the bar asked each of its reads for five.
LIMIT = 5


class SearchHit(BaseModel):
    id: str
    title: str
    priority: Optional[str] = None
    status: Optional[str] = None
    data_source: Optional[str] = None
    severity: Optional[str] = None
    description: Optional[str] = None
    # What a case matched on, when not its title or description.
    match_type: Optional[Literal["case", "comment", "evidence", "alert"]] = None


class SearchResults(BaseModel):
    case_exact: Optional[SearchHit] = None
    finding_exact: Optional[SearchHit] = None
    cases: List[SearchHit] = Field(default_factory=list)
    alerts: List[SearchHit] = Field(default_factory=list)
    workflows: List[SearchHit] = Field(default_factory=list)
    agents: List[SearchHit] = Field(default_factory=list)
    skills: List[SearchHit] = Field(default_factory=list)


def _matches(q: str, *fields: Optional[str]) -> bool:
    """Every whitespace/comma-separated term appears in one of ``fields``."""
    text = " ".join(field for field in fields if field).lower()
    return all(term in text for term in re.split(r"[\s,]+", q.lower()) if term)


def _alert(row: Optional[dict]) -> Optional[SearchHit]:
    if not row or not row.get("finding_id"):
        return None
    return SearchHit(
        id=row["finding_id"],
        title=row.get("title") or row.get("description") or row["finding_id"],
        data_source=row.get("data_source"),
        severity=row.get("severity"),
    )


def _cases(q: str) -> List[SearchHit]:
    """Title, description, comment and evidence matches, then IOC matches; each case once."""
    case_search = CaseSearchService()
    text = case_search.search_full_text(q, limit=LIMIT)
    rows = text["cases"] + text["comments"] + text["evidence"]
    hits, seen = [], set()
    for row in rows + case_search.search_by_ioc(q):
        if row["case_id"] in seen:
            continue
        seen.add(row["case_id"])
        label = row.get("title") or row.get("name") or row.get("content")
        hits.append(
            SearchHit(
                id=row["case_id"],
                title=label or row["case_id"],
                priority=row.get("priority"),
                status=row.get("status"),
                match_type=row.get("match_type"),
            )
        )
    return hits


@router.get("", response_model=SearchResults)
def search(
    q: str = Query(..., min_length=1, max_length=200),
    user: User = Depends(get_current_user),
    workflows: WorkflowsService = Depends(provide_workflows),
):
    """Cases, alerts, workflows, agents and skills matching ``q``, for the command bar."""
    q = q.strip()
    with unit_of_work() as session:
        allowed = {
            permission
            for permission in ("cases.read", "findings.read", "automation.read")
            if missing_permission(user.user_id, permission, session) is None
        }
    results = SearchResults()
    data_service = DatabaseDataService()
    if "cases.read" in allowed:
        case = data_service.get_case(q)
        if case:
            results.case_exact = SearchHit(
                id=case["case_id"],
                title=case.get("title") or case["case_id"],
                priority=case.get("priority"),
                status=case.get("status"),
            )
    if "findings.read" in allowed:
        results.finding_exact = _alert(data_service.get_finding(q))
    if len(q) < MIN_SEARCH_CHARS:
        return results
    if "cases.read" in allowed:
        results.cases = _cases(q)
    if "findings.read" in allowed:
        # Matches entity_context too, so a host, user or IP finds its alerts.
        found = data_service.get_findings(search_query=q, limit=LIMIT)
        results.alerts = [hit for hit in map(_alert, found) if hit]
    if "automation.read" in allowed:
        agent_manager.refresh_custom_agents()
        results.workflows = [
            SearchHit(
                id=w["id"],
                title=w.get("name") or w["id"],
                description=w.get("description"),
            )
            for w in workflows.list_workflows()
            if _matches(q, w["id"], w.get("name"), w.get("description"))
        ][:LIMIT]
        results.agents = [
            SearchHit(id=a.id, title=a.name, description=a.description)
            for a in agent_manager.agents.values()
            if _matches(q, a.id, a.name, a.description, a.specialization)
        ][:LIMIT]
        results.skills = [
            SearchHit(id=s.name, title=s.name, description=s.description)
            for s in load_skills(skill_roots())
            if _matches(q, s.name, s.description)
        ][:LIMIT]
    return results
