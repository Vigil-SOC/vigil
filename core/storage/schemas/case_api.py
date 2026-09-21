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
