"""Repository for Case aggregates.

Owns the single case query builder and the case<->finding link logic, so the
divergent ``DatabaseService.get_cases`` and ``CaseSearchService.search_cases``
implementations collapse into one place. Operates on a caller-provided
``Session`` (wrap it with ``services.unit_of_work.unit_of_work``); it never
opens or closes sessions itself.
"""

from datetime import datetime
from typing import List, Optional, Sequence, Tuple, Union

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from core.storage.models import Case, CaseSLA, Finding

# status / priority / assignee accept either a single value or a list.
Filterable = Optional[Union[str, Sequence[str]]]


def _as_list(value: Filterable) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


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
        return stmt

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
        limit: int = 100,
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
