"""API envelopes and computed payloads for the cases router.

Entity rows reuse the dump schemas; each list wrapper and each computed dict
is its own model so OpenAPI records the real JSON. Lives here rather than
under ``services/api/routers/`` because every module there must export a
``router``.
"""

from typing import Any, Optional

from pydantic import BaseModel

from core.storage.schemas.case import CaseSchema
from core.storage.schemas.case_entities import (
    CaseClosureInfoSchema,
    CaseCommentSchema,
    CaseEscalationSchema,
    CaseEvidenceSchema,
    CaseIOCSchema,
    CaseRelationshipSchema,
    CaseTaskSchema,
    CaseWatcherSchema,
)


class CaseSuccessResponse(BaseModel):
    """Mutation that only reports whether it landed."""

    success: bool


class CaseListResponse(BaseModel):
    cases: list[CaseSchema]
    total: int


class CaseInvestigationRef(BaseModel):
    """One investigation on the case page. Newest first on the detail read."""

    investigation_id: str
    status: str
    workflow_id: str
    run_id: str
    live: bool = False
    cost_usd: float = 0
    max_cost_usd: float = 0
    budget_health: str = "healthy"
    iteration_count: int = 0
    created_at: Optional[str] = None


class CaseClosureView(BaseModel):
    """What the closed summary shows. ``verdict`` is the stated reason."""

    closure_category: str
    closed_by: str
    closed_by_kind: str
    verdict: str = ""


class CaseDetailSchema(CaseSchema):
    """``GET /cases/{id}``. The list stays ``CaseSchema``."""

    combined_state: str = ""
    investigations: list[CaseInvestigationRef] = []
    closure: Optional[CaseClosureView] = None


class CaseRecordRow(BaseModel):
    id: str
    at: str
    kind: str
    source: str
    chained: bool
    text: str


class CaseRecordResponse(BaseModel):
    """The merged record. ``run_id`` is absent when the case has no investigation."""

    run_id: Optional[str] = None
    investigation_id: Optional[str] = None
    rows: list[CaseRecordRow] = []


class CasePurgeResponse(BaseModel):
    success: bool
    deleted: int
    killed_investigations: int = 0
    message: str


class CaseReportResponse(BaseModel):
    success: bool
    filename: str
    path: str
    case_id: str


class CaseSummaryResponse(BaseModel):
    total: int
    by_status: dict[str, int]
    by_priority: dict[str, int]


class CaseCommentsResponse(BaseModel):
    comments: list[CaseCommentSchema]


class CaseWatchersResponse(BaseModel):
    watchers: list[CaseWatcherSchema]


class CaseEvidenceListResponse(BaseModel):
    evidence: list[CaseEvidenceSchema]


class CaseIOCListResponse(BaseModel):
    iocs: list[CaseIOCSchema]


class CaseIOCBulkResponse(BaseModel):
    added: int


class CaseIOCExportResponse(BaseModel):
    format: str
    content: Any


class CaseTasksResponse(BaseModel):
    tasks: list[CaseTaskSchema]


class CaseRelationshipsResponse(BaseModel):
    relationships: list[CaseRelationshipSchema]


class CaseCloseResponse(BaseModel):
    success: bool
    closure: CaseClosureInfoSchema


class CaseEscalationsResponse(BaseModel):
    escalations: list[CaseEscalationSchema]


class CaseMergeResponse(BaseModel):
    success: bool
    target_case: Optional[CaseSchema] = None
    findings_moved: Optional[int] = None
    source_case_status: str
    message: str


class CaseSearchResponse(BaseModel):
    results: list[CaseSchema]
    total: int
    limit: int
    offset: int
    has_more: bool
