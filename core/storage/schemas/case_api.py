"""API envelopes and computed payloads for the cases router.

Entity rows reuse the dump schemas; each list wrapper and each computed dict
is its own model so OpenAPI records the real JSON. Lives here rather than
under ``services/api/routers/`` because every module there must export a
``router``.
"""

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel

from core.storage.schemas.case import CaseSchema, CaseTeam
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

# Whether a case's or task's stored owner is a current person (core/cases/assignee.py);
# null when unassigned. Required, so a read that forgets to fill it fails loudly.
OwnerStateField = Optional[Literal["current", "not_current"]]


class CaseSuccessResponse(BaseModel):
    """Mutation that only reports whether it landed."""

    success: bool


class CaseFieldConflict(BaseModel):
    """One field whose stored value is not what the caller last saw."""

    field: Literal["title", "description", "status", "priority", "assignee"]
    expected: Optional[str] = None
    current: Optional[str] = None


class CaseConflictResponse(BaseModel):
    """A refused edit. Nothing was written."""

    detail: str
    conflicts: List[CaseFieldConflict]


class CaseQueueItem(BaseModel):
    """One row of the case queue.

    ``case_id`` and ``title`` stay so callers that only need a picker
    (the workflow start dialog) can keep reading the list.
    """

    case_id: str
    title: str
    priority: Optional[str] = None
    assignee: Optional[str] = None
    combined_state: str
    workflow_id: Optional[str] = None
    findings_count: int = 0
    iteration_count: Optional[int] = None
    cost_usd: Optional[float] = None
    max_cost_usd: Optional[float] = None
    budget_health: Optional[str] = None
    comment_count: int = 0
    last_activity: Optional[datetime] = None
    age_seconds: float
    sla_seconds_left: Optional[float] = None
    health_status: Optional[str] = None
    needs_you: bool = False
    description: Optional[str] = None
    sla_paused: bool = False
    owner_state: OwnerStateField
    # True when the owner is on the case's team, false when they are not,
    # null when the case has no owner.
    owner_on_team: Optional[bool] = None
    # Restricted and owned: true when the owner can't read the case ("owner
    # can't see this case"), worked out at read. Null otherwise.
    owner_cannot_read: Optional[bool] = None
    team: Optional[CaseTeam] = None
    # Restricted cases (#4354): shown only to readers, with the reader list.
    restricted: bool = False
    readers: list[str] = []


class CaseQueueStrip(BaseModel):
    """Counts for the queue strip. Independent of the page filters."""

    by_state: dict[str, int]
    sla_at_risk: int
    closed_today: int
    agent_closure_share: float
    needs_you: int
    # Unowned cases with no assigned or executing run, plus cases whose live
    # run is waiting on a person. Open cases only.
    waiting: int


class CaseListResponse(BaseModel):
    cases: list[CaseQueueItem]
    total: int
    limit: int
    offset: int
    has_more: bool
    strip: CaseQueueStrip
    # Restricted cases the caller may not read, left out. Never filtered, so it
    # says nothing about what those cases hold.
    restricted_not_counted: int = 0


class CaseOwner(BaseModel):
    """One stored owner on a readable case: the value as stored, and its state."""

    owner: str
    owner_state: Literal["current", "not_current"]


class CaseOwnersResponse(BaseModel):
    """Distinct non-blank owners on cases the reader can list, closed included."""

    owners: list[CaseOwner]


class CaseInvestigationRef(BaseModel):
    """One run on the case page: an investigation, or a run started on the case
    with no investigation row (``investigation_id`` null). Newest first."""

    investigation_id: Optional[str] = None
    status: str
    workflow_id: str
    # As the run recorded it. A run with no entry in the workflow list has only this.
    workflow_name: Optional[str] = None
    run_id: str
    live: bool = False
    cost_usd: float = 0
    max_cost_usd: float = 0
    budget_health: str = "healthy"
    iteration_count: int = 0
    created_at: Optional[str] = None
    # The case's run: the one the page reads, not a follow-up a handoff started.
    primary: bool = False


class CaseClosureView(BaseModel):
    """What the closed summary shows. ``verdict`` is the stated reason."""

    closure_category: str
    closed_by: str
    closed_by_kind: str
    closed_at: Optional[str] = None
    verdict: str = ""


class CaseLinkedFinding(BaseModel):
    """One finding the case already links, and the source door when one exists."""

    finding_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    source_link: Optional[str] = None
    entities: list[str] = []
    # Typed ``type:value`` keys, as the orchestrator mints a run's recall keys.
    entity_keys: list[str] = []


class CaseDetailResponse(CaseSchema):
    """``GET /cases/{id}`` — the case, its combined state, and the runs on it."""

    combined_state: str
    investigations: list[CaseInvestigationRef] = []
    closure: Optional[CaseClosureView] = None
    linked_findings: list[CaseLinkedFinding] = []
    # Merged after the case's newest run started, so that run did not read them.
    alerts_since_last_run: int = 0
    owner_state: OwnerStateField
    owner_on_team: Optional[bool] = None
    owner_cannot_read: Optional[bool] = None


class CaseRuleCard(BaseModel):
    """One rule version behind a case's alerts, and its stored checks."""

    rule_id: int
    version: int
    latest_version: Optional[int] = None
    name: str
    source: str
    tool: str
    native_key: str
    status: str
    # runs, sees, quiet, accurate, noisy, silent, caught, drift: as stored.
    checks: dict[str, dict[str, Any]] = {}
    flags: list[str] = []
    caught: bool = False
    health: str
    worst: str
    # The overlay copy's current stage (shadow, live, off); None elsewhere.
    stage: Optional[str] = None
    findings: list[str] = []
    # Of ``findings``, those traced only by the rule name or title they carry.
    by_name: int = 0


class CaseUnresolvedFinding(BaseModel):
    finding_id: str
    reason: str


class CaseRulesResponse(BaseModel):
    """``GET /cases/{id}/rules`` — worst first; alerts traced to no rule apart."""

    rules: list[CaseRuleCard] = []
    unresolved: int = 0
    unresolved_findings: list[CaseUnresolvedFinding] = []


class CaseRecordRow(BaseModel):
    id: str
    at: str
    kind: str
    source: str
    chained: bool
    text: str
    # The run a run row is from; absent on audit and activity rows.
    run_id: Optional[str] = None


class CaseRecordResponse(BaseModel):
    """The merged record. ``run_id`` is absent when the case has no run."""

    run_id: Optional[str] = None
    investigation_id: Optional[str] = None
    rows: list[CaseRecordRow] = []


class CasePurgeResponse(BaseModel):
    success: bool
    deleted: int
    killed_investigations: int = 0
    # Restricted cases the caller may not read, left in place (#4354).
    restricted_kept: int = 0
    message: str


class CaseSummaryResponse(BaseModel):
    total: int
    by_status: dict[str, int]
    by_priority: dict[str, int]
    # Restricted cases the caller may not read, left out of every count above.
    restricted_not_counted: int = 0


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


class CaseTaskRead(CaseTaskSchema):
    owner_state: OwnerStateField


class CaseTasksResponse(BaseModel):
    tasks: list[CaseTaskRead]


class CaseRelatedSchema(CaseRelationshipSchema):
    """A link with the related Case's title and status, for the side panel."""

    related_title: Optional[str] = None
    related_status: Optional[str] = None


class CaseRelationshipsResponse(BaseModel):
    relationships: list[CaseRelatedSchema]


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


class CaseSearchResult(CaseSchema):
    owner_state: OwnerStateField


class CaseSearchResponse(BaseModel):
    results: list[CaseSearchResult]
    total: int
    limit: int
    offset: int
    has_more: bool
