"""Advanced Case Search API endpoints."""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from core.cases.case_search_service import CaseSearchService
from core.routing import Auth, RouterMeta

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/cases/search",
    tags=["case-search"],
    auth=Auth.REQUIRED,
)
search_service = CaseSearchService()


class AdvancedSearch(BaseModel):
    """Advanced search request."""

    query_text: Optional[str] = None
    status: Optional[List[str]] = None
    priority: Optional[List[str]] = None
    assignee: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    mitre_techniques: Optional[List[str]] = None
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    updated_after: Optional[datetime] = None
    updated_before: Optional[datetime] = None
    has_sla_breach: Optional[bool] = None
    limit: int = 100
    offset: int = 0


@router.post("/advanced")
async def advanced_search(data: AdvancedSearch):
    """
    Perform advanced case search.

    Args:
        data: Search parameters

    Returns:
        Search results with pagination
    """
    results = search_service.search_cases(
        query_text=data.query_text,
        status=data.status,
        priority=data.priority,
        assignee=data.assignee,
        tags=data.tags,
        mitre_techniques=data.mitre_techniques,
        created_after=data.created_after,
        created_before=data.created_before,
        updated_after=data.updated_after,
        updated_before=data.updated_before,
        has_sla_breach=data.has_sla_breach,
        limit=data.limit,
        offset=data.offset,
    )
    return results


@router.get("/full-text")
async def full_text_search(
    query: str,
    search_comments: bool = True,
    search_evidence: bool = True,
    limit: int = 50,
):
    """
    Full-text search across cases and related entities.

    Args:
        query: Search query
        search_comments: Include comments
        search_evidence: Include evidence
        limit: Maximum results

    Returns:
        Search results grouped by type
    """
    results = search_service.search_full_text(
        query, search_comments, search_evidence, limit
    )
    return results


@router.get("/by-ioc")
async def search_by_ioc(ioc_value: str, ioc_type: Optional[str] = None):
    """
    Find cases containing a specific IOC.

    Args:
        ioc_value: IOC value to search
        ioc_type: Optional IOC type filter

    Returns:
        Cases containing the IOC
    """
    cases = search_service.search_by_ioc(ioc_value, ioc_type)
    return {"cases": cases}


@router.get("/related/{case_id}")
async def get_related_cases(case_id: str, max_results: int = 10):
    """
    Find cases related to a given case.

    Args:
        case_id: Case ID
        max_results: Maximum results to return

    Returns:
        Related cases with similarity scores
    """
    related = search_service.get_related_cases(case_id, max_results)
    return {"related_cases": related}
