"""Find the open Case already holding a finding on a given host pair.

Repeats of one pair arrive every delivery. A finding joins the newest open
Case created inside the window that already holds a finding on the same pair,
in either direction. Operates on a caller-provided ``Session``.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from core.storage.models import Case, Finding, Investigation
from core.storage.models.base import case_findings
from core.storage.shared_ioc_repository import _live_clause
from core.time import utcnow

CLOSED_CASE_STATUSES = ("resolved", "closed")


@dataclass(frozen=True)
class PairCase:
    case_id: str
    severities: Tuple[str, ...]
    has_live_run: bool


def open_pair_case(
    session: Session,
    data_source: str,
    pair: Tuple[str, str],
    since: datetime,
) -> Optional[PairCase]:
    """Newest open Case created at or after ``since`` with a ``data_source`` finding on ``pair``."""
    a, b = pair
    src = Finding.entity_context["src_ip"].astext
    dst = Finding.entity_context["dst_ip"].astext
    case_id = session.execute(
        select(Case.case_id)
        .join(case_findings, case_findings.c.case_id == Case.case_id)
        .join(Finding, Finding.finding_id == case_findings.c.finding_id)
        .where(
            Case.created_at >= since,
            Case.status.notin_(CLOSED_CASE_STATUSES),
            Finding.data_source == data_source,
            or_(and_(src == a, dst == b), and_(src == b, dst == a)),
        )
        .order_by(Case.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if case_id is None:
        return None

    severities: List[str] = [
        (s or "").lower()
        for s in session.execute(
            select(Finding.severity)
            .join(case_findings, case_findings.c.finding_id == Finding.finding_id)
            .where(case_findings.c.case_id == case_id)
        ).scalars()
    ]
    live = session.execute(
        select(Investigation.investigation_id)
        .where(Investigation.case_id == case_id, _live_clause(utcnow()))
        .limit(1)
    ).scalar_one_or_none()
    return PairCase(case_id, tuple(severities), live is not None)
