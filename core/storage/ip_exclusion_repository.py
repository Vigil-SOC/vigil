"""Repository for ``ip_exclusions`` and the SQL predicate that applies them to
``findings``.

The predicate lives here, in the storage tier, because the findings queries in
``core.storage.service`` apply it and storage may not import a capability
domain. Validation and the rules about *when* an exclusion applies are in
``core.findings.exclusions``. Operates on a caller-provided ``Session``; it
never opens, commits or closes one.
"""

from typing import Dict, List, Optional, Tuple

from sqlalchemy import (
    ColumnElement,
    Text,
    case,
    cast,
    distinct,
    false,
    func,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.orm import Session

from core.storage.models import Finding, IpExclusion

# How a findings query treats findings that name an excluded address.
EXCLUSION_VIEWS = ("include", "hide", "only")


def active_ips_subquery():
    """``text[]`` of every actively excluded address, evaluated once per query."""
    return (
        select(
            func.coalesce(
                func.array_agg(cast(IpExclusion.ip, Text)),
                cast([], ARRAY(Text)),
            )
        )
        .where(IpExclusion.removed_at.is_(None))
        .scalar_subquery()
    )


def finding_names_any(ips) -> ColumnElement[bool]:
    """SQL: the finding names any of ``ips`` (a ``text[]`` expression).

    Reads ``findings.ip_addresses``, which the GIN index serves. Coalesced so a
    finding without addresses is false rather than NULL: ``NOT NULL`` would drop
    it from the hidden view as well.
    """
    return func.coalesce(Finding.ip_addresses.op("&&")(ips), false())


def exclusion_view_filter(view: str) -> Optional[ColumnElement[bool]]:
    """WHERE clause for one of :data:`EXCLUSION_VIEWS`; ``None`` means no filter."""
    if view not in EXCLUSION_VIEWS:
        raise ValueError(f"exclusions must be one of {EXCLUSION_VIEWS}; got {view!r}")
    if view == "include":
        return None
    if view == "only":
        # Bare, so the GIN index serves it. Nothing active is ``&& '{}'``: false.
        return Finding.ip_addresses.op("&&")(active_ips_subquery())
    # CASE, not OR: Postgres may evaluate both arms of OR, and the skipped arm
    # is not run when nothing is excluded. A row filter rather than an anti-join
    # against the index: it costs per candidate row, not per excluded finding.
    none_active = func.cardinality(active_ips_subquery()) == 0
    return case((none_active, true()), else_=~finding_names_any(active_ips_subquery()))


def count_findings_per_active_ip(session: Session) -> Tuple[Dict[str, int], int]:
    """``({address: findings naming it}, findings naming any)`` in one query:
    each address finds its findings through the ``ip_addresses`` index. ROLLUP
    adds the total as the row whose address is NULL. Nothing active means
    nothing is hidden, and the findings scan is skipped."""
    if (
        session.execute(
            select(IpExclusion.ip).where(IpExclusion.removed_at.is_(None)).limit(1)
        ).first()
        is None
    ):
        return {}, 0
    active = (
        select(IpExclusion.ip.label("ip"))
        .where(IpExclusion.removed_at.is_(None))
        .subquery()
    )
    rows = session.execute(
        select(active.c.ip, func.count(distinct(Finding.finding_id)))
        .select_from(
            active.join(
                Finding,
                Finding.ip_addresses.op("&&")(array([cast(active.c.ip, Text)])),
            )
        )
        .group_by(func.rollup(active.c.ip))
    ).all()
    per_ip = {ip: count for ip, count in rows if ip is not None}
    total = next((count for ip, count in rows if ip is None), 0)
    return per_ip, total


def list_rows(session: Session, *, include_removed: bool = False) -> List[IpExclusion]:
    query = select(IpExclusion).order_by(IpExclusion.created_at.desc())
    if not include_removed:
        query = query.where(IpExclusion.removed_at.is_(None))
    return list(session.execute(query).scalars().all())


def active_row_for(session: Session, ip: str) -> Optional[IpExclusion]:
    return session.execute(
        select(IpExclusion).where(
            IpExclusion.ip == ip, IpExclusion.removed_at.is_(None)
        )
    ).scalar_one_or_none()


def active_ips(session: Session) -> List[str]:
    return list(
        session.execute(
            select(IpExclusion.ip).where(IpExclusion.removed_at.is_(None))
        ).scalars()
    )
