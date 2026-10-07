"""Repository for Case aggregates.

Owns the single case query builder and the case<->finding link logic, so the
divergent ``DatabaseService.get_cases`` and ``CaseSearchService.search_cases``
implementations collapse into one place. Operates on a caller-provided
``Session`` (wrap it with ``services.unit_of_work.unit_of_work``); it never
opens or closes sessions itself.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple, Union

from sqlalchemy import Select, and_, case, exists, func, or_, select
from sqlalchemy.orm import Session

from core.storage.models import (
    Case,
    CaseClosureInfo,
    CaseComment,
    CaseSLA,
    Finding,
    Investigation,
    case_findings,
)
from core.storage.models.workflow import LIVE_INVESTIGATION_STATUSES
from core.time import utcnow

# status / priority / assignee accept either a single value or a list.
Filterable = Optional[Union[str, Sequence[str]]]

# Default page size for ``search`` and the case queue.
PAGE_LIMIT = 100

# Same 75% elapsed cut as ``CaseSLAService.get_sla_status``. Stored here so the
# queue filter does not import the cases domain.
_SLA_AT_RISK = 0.75

# ``case_closure_info.closed_by_kind`` value for an agent close, as stored.
_CLOSED_BY_AGENT = "agent"


def _as_list(value: Filterable) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _ranked(stmt: Select, name: str):
    """One row per case: the innermost select is windowed, this keeps ``rn = 1``."""
    ranked = stmt.subquery()
    return select(*ranked.c).where(ranked.c.rn == 1).subquery(name)


def _latest_investigations():
    return _ranked(
        select(
            Investigation.case_id.label("inv_case_id"),
            Investigation.workflow_id.label("workflow_id"),
            Investigation.iteration_count.label("iteration_count"),
            Investigation.cost_usd.label("cost_usd"),
            Investigation.max_cost_usd.label("max_cost_usd"),
            Investigation.last_activity_at.label("inv_last_activity_at"),
            func.row_number()
            .over(
                partition_by=Investigation.case_id,
                order_by=(
                    Investigation.created_at.desc(),
                    Investigation.investigation_id.desc(),
                ),
            )
            .label("rn"),
        ).where(Investigation.case_id.isnot(None)),
        "latest_investigation",
    )


def _live_investigations():
    return _ranked(
        select(
            Investigation.case_id.label("live_case_id"),
            Investigation.status.label("live_status"),
            func.row_number()
            .over(
                partition_by=Investigation.case_id,
                order_by=(
                    Investigation.created_at.desc(),
                    Investigation.investigation_id.desc(),
                ),
            )
            .label("rn"),
        ).where(
            Investigation.case_id.isnot(None),
            Investigation.status.in_(LIVE_INVESTIGATION_STATUSES),
        ),
        "live_investigation",
    )


def _latest_slas():
    return _ranked(
        select(
            CaseSLA.case_id.label("sla_case_id"),
            CaseSLA.resolution_due.label("resolution_due"),
            CaseSLA.response_due.label("response_due"),
            CaseSLA.response_completed_at.label("response_completed_at"),
            CaseSLA.resolution_completed_at.label("resolution_completed_at"),
            CaseSLA.is_paused.label("is_paused"),
            CaseSLA.created_at.label("sla_created_at"),
            func.row_number()
            .over(
                partition_by=CaseSLA.case_id,
                order_by=(CaseSLA.created_at.desc(), CaseSLA.sla_id.desc()),
            )
            .label("rn"),
        ),
        "latest_sla",
    )


def _comment_counts():
    return (
        select(
            CaseComment.case_id.label("comment_case_id"),
            func.count().label("comment_count"),
        )
        .group_by(CaseComment.case_id)
        .subquery("comment_counts")
    )


def _finding_counts():
    return (
        select(
            case_findings.c.case_id.label("finding_case_id"),
            func.count().label("findings_count"),
        )
        .group_by(case_findings.c.case_id)
        .subquery("finding_counts")
    )


def _combined_sql(live):
    return case(
        (Case.status == "closed", "closed"),
        (live.c.live_status.isnot(None), live.c.live_status),
        else_=Case.status,
    )


def _open_clock_at_risk(due, completed, created, now):
    """True when an open clock has elapsed at least the warning cut.

    ``elapsed / total >= 0.75`` written as a product so a zero-length clock
    (the ``total > 0`` guard) stays out, matching ``get_sla_status``.
    """
    total = func.extract("epoch", due - created)
    elapsed = func.extract("epoch", now - created)
    return and_(
        completed.is_(None),
        total > 0,
        elapsed >= total * _SLA_AT_RISK,
    )


def _sla_at_risk(sla, now):
    """SQL health other than ``healthy``: past due, or elapsed past 75%.

    A paused SLA is healthy. A case with no SLA row is not at risk.
    """
    past_due = or_(
        and_(sla.c.response_completed_at.is_(None), now > sla.c.response_due),
        and_(
            sla.c.resolution_completed_at.is_(None),
            now > sla.c.resolution_due,
        ),
    )
    elapsed = or_(
        _open_clock_at_risk(
            sla.c.response_due,
            sla.c.response_completed_at,
            sla.c.sla_created_at,
            now,
        ),
        _open_clock_at_risk(
            sla.c.resolution_due,
            sla.c.resolution_completed_at,
            sla.c.sla_created_at,
            now,
        ),
    )
    return and_(
        sla.c.sla_case_id.isnot(None),
        sla.c.is_paused.is_(False),
        or_(past_due, elapsed),
    )


def _finding_source_exists(data_source: str):
    return exists(
        select(Finding.finding_id)
        .select_from(case_findings)
        .join(Finding, Finding.finding_id == case_findings.c.finding_id)
        .where(
            case_findings.c.case_id == Case.case_id,
            Finding.data_source == data_source,
        )
    )


@dataclass(frozen=True)
class CaseQueueRow:
    """One queue row before combined state and the health words.

    Those are applied in ``core.cases.combined_state`` so this module does not
    import the cases domain.
    """

    case_id: str
    title: str
    priority: Optional[str]
    assignee: Optional[str]
    status: str
    live_status: Optional[str]
    workflow_id: Optional[str]
    findings_count: int
    iteration_count: Optional[int]
    cost_usd: Optional[float]
    max_cost_usd: Optional[float]
    comment_count: int
    last_activity: Optional[datetime]
    age_seconds: float
    has_sla: bool
    sla_created_at: Optional[datetime]
    response_due: Optional[datetime]
    resolution_due: Optional[datetime]
    response_completed_at: Optional[datetime]
    resolution_completed_at: Optional[datetime]
    is_paused: bool


@dataclass(frozen=True)
class CaseQueueStrip:
    by_state: dict
    sla_at_risk: int
    closed_today: int
    agent_closure_share: float


class CaseRepository:
    """Data access for cases over an existing SQLAlchemy session."""

    def __init__(self, session: Session):
        self.session = session

    # ---- findings link -------------------------------------------------

    def resolve_findings(self, finding_ids: Sequence[str]) -> List[Finding]:
        """Load Finding objects for the given ids (empty list if none)."""
        if not finding_ids:
            return []
        return list(
            self.session.execute(
                select(Finding).where(Finding.finding_id.in_(finding_ids))
            )
            .scalars()
            .all()
        )

    def set_findings(self, case: Case, finding_ids: Sequence[str]) -> None:
        """Replace a case's linked findings.

        ``finding_ids`` is not a mapped column — the link is the ``findings``
        relationship (many-to-many via ``case_findings``). Assigning it here is
        what keeps case<->finding links from being silently dropped on writes.
        """
        case.findings = self.resolve_findings(finding_ids)

    # ---- reads ---------------------------------------------------------

    def get(self, case_id: str, include_findings: bool = False) -> Optional[Case]:
        case = self.session.get(Case, case_id)
        if case is not None and include_findings:
            _ = case.findings  # force the lazy load while the session is open
        return case

    def _build(
        self,
        *,
        query_text: Optional[str] = None,
        status: Filterable = None,
        priority: Filterable = None,
        assignee: Filterable = None,
        tags: Optional[Sequence[str]] = None,
        mitre_techniques: Optional[Sequence[str]] = None,
        created_after: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
        updated_after: Optional[datetime] = None,
        updated_before: Optional[datetime] = None,
        has_sla_breach: Optional[bool] = None,
        workflow: Optional[str] = None,
        data_source: Optional[str] = None,
        sla_at_risk: bool = False,
        state: Optional[str] = None,
        closed: Optional[bool] = None,
        open_only: bool = False,
        now: Optional[datetime] = None,
    ) -> Select:
        """Build the filtered (unordered, unpaginated) case SELECT."""
        stmt: Select = select(Case)

        if has_sla_breach is not None:
            if has_sla_breach:
                stmt = stmt.join(CaseSLA).where(CaseSLA.breached.is_(True))
            else:
                stmt = stmt.outerjoin(CaseSLA).where(
                    or_(CaseSLA.breached.is_(False), CaseSLA.case_id.is_(None))
                )

        conditions = []
        if query_text:
            pattern = f"%{query_text}%"
            conditions.append(
                or_(Case.title.ilike(pattern), Case.description.ilike(pattern))
            )

        for column, value in (
            (Case.status, status),
            (Case.priority, priority),
            (Case.assignee, assignee),
        ):
            values = _as_list(value)
            if values:
                conditions.append(column.in_(values))

        # Array-contains filters: a case must carry every requested tag/technique.
        for tag in _as_list(tags):
            conditions.append(Case.tags.contains([tag]))
        for technique in _as_list(mitre_techniques):
            conditions.append(Case.mitre_techniques.contains([technique]))

        if created_after:
            conditions.append(Case.created_at >= created_after)
        if created_before:
            conditions.append(Case.created_at <= created_before)
        if updated_after:
            conditions.append(Case.updated_at >= updated_after)
        if updated_before:
            conditions.append(Case.updated_at <= updated_before)

        if conditions:
            stmt = stmt.where(and_(*conditions))
        return self._restrict(
            stmt,
            workflow=workflow,
            data_source=data_source,
            sla_at_risk=sla_at_risk,
            state=state,
            closed=closed,
            open_only=open_only,
            now=now or utcnow(),
        )

    def find(
        self,
        *,
        limit: int = 1000,
        offset: int = 0,
        order_by: str = "updated_at",
        **filters,
    ) -> List[Case]:
        """Return matching cases (no total count)."""
        stmt = self._build(**filters)
        order_column = Case.created_at if order_by == "created_at" else Case.updated_at
        stmt = stmt.order_by(order_column.desc()).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars().all())

    def search(
        self,
        *,
        limit: int = PAGE_LIMIT,
        offset: int = 0,
        order_by: str = "updated_at",
        **filters,
    ) -> Tuple[List[Case], int]:
        """Return ``(cases, total_count)`` for the given filters."""
        stmt = self._build(**filters)
        total = self.session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
        cases = self.find(limit=limit, offset=offset, order_by=order_by, **filters)
        return cases, int(total)

    def summary_counts(self) -> Tuple[int, Dict[str, int], Dict[str, int]]:
        """``(total, by_status, by_priority)`` over every case, in SQL.

        Counts and groups in the database, so the numbers cover the whole
        table rather than a loaded page of rows (#1438).
        """
        total = self.session.execute(select(func.count()).select_from(Case)).scalar()
        return (
            int(total or 0),
            self._grouped_counts(Case.status),
            self._grouped_counts(Case.priority),
        )

    def _grouped_counts(self, column) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        stmt = select(column, func.count()).group_by(column)
        for value, count in self.session.execute(stmt).all():
            key = "unknown" if value is None else value
            counts[key] = counts.get(key, 0) + int(count)
        return counts

    def latest_live_status(self, case_id: str) -> Optional[str]:
        """Status of the latest live investigation on this case, if any."""
        stmt = (
            select(Investigation.status)
            .where(
                Investigation.case_id == case_id,
                Investigation.status.in_(LIVE_INVESTIGATION_STATUSES),
            )
            .order_by(
                Investigation.created_at.desc(),
                Investigation.investigation_id.desc(),
            )
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def _restrict(
        self,
        stmt: Select,
        *,
        workflow: Optional[str],
        data_source: Optional[str],
        sla_at_risk: bool,
        state: Optional[str],
        closed: Optional[bool],
        open_only: bool,
        now: datetime,
        live=None,
        latest=None,
        sla=None,
    ) -> Select:
        """Queue filters. Joins are added only when the caller has not already."""
        if state and live is None:
            live = _live_investigations()
            stmt = stmt.outerjoin(live, live.c.live_case_id == Case.case_id)
        if workflow and latest is None:
            latest = _latest_investigations()
            stmt = stmt.outerjoin(latest, latest.c.inv_case_id == Case.case_id)
        if sla_at_risk and sla is None:
            sla = _latest_slas()
            stmt = stmt.outerjoin(sla, sla.c.sla_case_id == Case.case_id)

        clauses = []
        if workflow:
            clauses.append(latest.c.workflow_id == workflow)
        if data_source:
            clauses.append(_finding_source_exists(data_source))
        if sla_at_risk:
            clauses.append(_sla_at_risk(sla, now))
        if closed is True:
            clauses.append(Case.status == "closed")
        elif state:
            clauses.append(_combined_sql(live) == state)
        elif open_only:
            clauses.append(Case.status != "closed")
        if clauses:
            stmt = stmt.where(and_(*clauses))
        return stmt

    def queue(
        self,
        *,
        limit: int = PAGE_LIMIT,
        offset: int = 0,
        query_text: Optional[str] = None,
        priority: Filterable = None,
        assignee: Filterable = None,
        workflow: Optional[str] = None,
        data_source: Optional[str] = None,
        sla_at_risk: bool = False,
        state: Optional[str] = None,
        closed: Optional[bool] = None,
        now: Optional[datetime] = None,
        needs_you_ids: Optional[set[str]] = None,
    ) -> Tuple[List[CaseQueueRow], int]:
        """One page of the queue.

        Ids in ``needs_you_ids`` sort first, then soonest resolution, no SLA
        last. The tie-break is last activity descending: the later of
        ``cases.updated_at`` and the latest investigation's
        ``last_activity_at``. An empty set keeps that SLA order and does not
        emit ``IN ()``. Closed cases are omitted unless ``closed`` is set or
        ``state`` names one.
        """
        now = now or utcnow()
        latest = _latest_investigations()
        live = _live_investigations()
        sla = _latest_slas()
        comments = _comment_counts()
        findings = _finding_counts()
        last_activity = func.greatest(Case.updated_at, latest.c.inv_last_activity_at)
        # A paused or completed resolution clock has no time left. Those rows
        # stay ahead of cases with no SLA, and behind every clock still running.
        clock_running = and_(
            sla.c.sla_case_id.isnot(None),
            sla.c.is_paused.is_(False),
            sla.c.resolution_completed_at.is_(None),
        )
        time_left = case(
            (clock_running, func.extract("epoch", sla.c.resolution_due - now)),
            else_=None,
        )
        has_sla = sla.c.sla_case_id.isnot(None)

        stmt = (
            select(
                Case.case_id,
                Case.title,
                Case.status,
                Case.priority,
                Case.assignee,
                Case.created_at,
                latest.c.workflow_id,
                latest.c.iteration_count,
                latest.c.cost_usd,
                latest.c.max_cost_usd,
                live.c.live_status,
                func.coalesce(comments.c.comment_count, 0).label("comment_count"),
                func.coalesce(findings.c.findings_count, 0).label("findings_count"),
                sla.c.sla_case_id,
                sla.c.resolution_due,
                sla.c.response_due,
                sla.c.response_completed_at,
                sla.c.resolution_completed_at,
                sla.c.is_paused,
                sla.c.sla_created_at,
                last_activity.label("last_activity"),
            )
            .select_from(Case)
            .outerjoin(latest, latest.c.inv_case_id == Case.case_id)
            .outerjoin(live, live.c.live_case_id == Case.case_id)
            .outerjoin(sla, sla.c.sla_case_id == Case.case_id)
            .outerjoin(comments, comments.c.comment_case_id == Case.case_id)
            .outerjoin(findings, findings.c.finding_case_id == Case.case_id)
        )
        # The queue box matches title, description, id, and assignee. Priority
        # and an exact assignee use the same predicates as ``_build``.
        if query_text:
            pattern = f"%{query_text}%"
            stmt = stmt.where(
                or_(
                    Case.title.ilike(pattern),
                    Case.description.ilike(pattern),
                    Case.case_id.ilike(pattern),
                    Case.assignee.ilike(pattern),
                )
            )
        if _as_list(priority):
            stmt = stmt.where(Case.priority.in_(_as_list(priority)))
        if _as_list(assignee):
            stmt = stmt.where(Case.assignee.in_(_as_list(assignee)))
        stmt = self._restrict(
            stmt,
            workflow=workflow,
            data_source=data_source,
            sla_at_risk=sla_at_risk,
            state=state,
            closed=closed,
            open_only=True,
            now=now,
            live=live,
            latest=latest,
            sla=sla,
        )
        total = self.session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
        # Membership is a 0/1 key so an empty set can omit it. ``IN ()`` is
        # invalid SQL, and a constant key would not change today's order.
        order = []
        if needs_you_ids:
            order.append(case((Case.case_id.in_(sorted(needs_you_ids)), 0), else_=1))
        order.extend(
            (
                has_sla.desc(),
                time_left.asc().nulls_last(),
                last_activity.desc().nulls_last(),
                Case.case_id.asc(),
            )
        )
        stmt = stmt.order_by(*order).limit(limit).offset(offset)
        rows = []
        for record in self.session.execute(stmt).mappings():
            created_at = record["created_at"]
            age = (now - created_at).total_seconds() if created_at else 0.0
            cost = record["cost_usd"]
            max_cost = record["max_cost_usd"]
            rows.append(
                CaseQueueRow(
                    case_id=record["case_id"],
                    title=record["title"],
                    priority=record["priority"],
                    assignee=record["assignee"],
                    status=record["status"],
                    live_status=record["live_status"],
                    workflow_id=record["workflow_id"],
                    findings_count=int(record["findings_count"] or 0),
                    iteration_count=(
                        None
                        if record["iteration_count"] is None
                        else int(record["iteration_count"])
                    ),
                    cost_usd=None if cost is None else float(cost),
                    max_cost_usd=None if max_cost is None else float(max_cost),
                    comment_count=int(record["comment_count"] or 0),
                    last_activity=record["last_activity"],
                    age_seconds=age,
                    has_sla=record["sla_case_id"] is not None,
                    sla_created_at=record["sla_created_at"],
                    response_due=record["response_due"],
                    resolution_due=record["resolution_due"],
                    response_completed_at=record["response_completed_at"],
                    resolution_completed_at=record["resolution_completed_at"],
                    is_paused=bool(record["is_paused"]),
                )
            )
        return rows, int(total)

    def strip(self, *, now: Optional[datetime] = None) -> CaseQueueStrip:
        """Open cases by combined state, SLA at risk, closures today.

        At risk is the same SQL predicate as the queue filter, over cases
        that are not closed. Today's closures are ``case_closure_info`` rows
        whose ``closed_at`` falls on the UTC day, and the agent share is the
        fraction of those with ``closed_by_kind == agent``.
        """
        now = now or utcnow()
        live = _live_investigations()
        combined = _combined_sql(live)
        grouped = self.session.execute(
            select(combined.label("state"), func.count())
            .select_from(Case)
            .outerjoin(live, live.c.live_case_id == Case.case_id)
            .where(Case.status != "closed")
            .group_by(combined)
        ).all()
        by_state = {state: int(count) for state, count in grouped if state}

        sla = _latest_slas()
        at_risk = self.session.execute(
            select(func.count())
            .select_from(Case)
            .outerjoin(sla, sla.c.sla_case_id == Case.case_id)
            .where(Case.status != "closed", _sla_at_risk(sla, now))
        ).scalar_one()

        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        today = (
            select(func.count())
            .select_from(CaseClosureInfo)
            .where(
                CaseClosureInfo.closed_at >= start,
                CaseClosureInfo.closed_at < end,
            )
        )
        closed_today = int(self.session.execute(today).scalar_one())
        agent_today = int(
            self.session.execute(
                today.where(CaseClosureInfo.closed_by_kind == _CLOSED_BY_AGENT)
            ).scalar_one()
        )
        share = (agent_today / closed_today) if closed_today else 0.0
        return CaseQueueStrip(
            by_state=by_state,
            sla_at_risk=int(at_risk),
            closed_today=closed_today,
            agent_closure_share=share,
        )
