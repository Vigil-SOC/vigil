"""Entity facts — versioned contract surface (``/api/v1/entity-facts``, #4743).

What Vigil holds from Entra, Okta and Falcon about users and hosts: per source
the rows held, their age, the hit rate and last failure over the last TTL, the
limits in force, and the newest rows. Read only: limits are raised through the
``entity_facts.limits`` system config, and rows leave by TTL or case close.

Gated on ``findings.read`` (epic #4719, D7). A row tied to a Case is shown, and
counted, only where that Case passes the restricted-case read rule.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from core.auth.permissions import permission_gate
from core.integrations import entity_facts
from core.routing import Auth, RouterMeta

router = APIRouter()
router.dependencies.append(permission_gate("findings.read"))

ROUTER_META = RouterMeta(
    prefix="/api/v1/entity-facts",
    tags=["entity-facts"],
    auth=Auth.REQUIRED,
)


class EntityFactFailure(BaseModel):
    at: datetime
    # The kind only (e.g. timeout, connect_failed): the source's error text is never kept.
    reason: str


class EntityFactSource(BaseModel):
    source: str
    label: str
    rows: int
    oldest_fetched_at: Optional[datetime] = None
    newest_fetched_at: Optional[datetime] = None
    # Over the last window_hours of reads: served from rows, fetched, or not answered.
    served: int
    fetched: int
    unavailable: int
    hit_rate: Optional[float] = None
    last_failure: Optional[EntityFactFailure] = None


class EntityFactRow(BaseModel):
    entity_key: str
    source: str
    case_id: Optional[str] = None
    # The source projection's allowlisted fields, as stored.
    fields: Dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime
    expires_at: datetime
    # Times served since it was last fetched.
    hit_count: int


class EntityFactsResponse(BaseModel):
    now: datetime
    window_hours: int
    limits: Dict[str, int]
    defaults: Dict[str, int]
    sources: List[EntityFactSource]
    rows_total: int
    rows: List[EntityFactRow]


@router.get("", response_model=EntityFactsResponse)
def get_entity_facts(limit: int = Query(100, ge=1, le=500)):
    return entity_facts.held(limit)
