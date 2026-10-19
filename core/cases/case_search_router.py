"""Advanced Case Search API endpoints."""

from typing import Optional

from fastapi import APIRouter

from core.cases.case_search_service import CaseSearchService
from core.routing import Auth, RouterMeta

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/cases/search",
    tags=["case-search"],
    auth=Auth.REQUIRED,
)
search_service = CaseSearchService()


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
