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
    or_,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.orm import Session

from core.storage.models import Finding, IpExclusion

# Top-level entity_context keys ingest writes addresses under. Each holds a
# string or a list of strings.
FINDING_IP_KEYS = (
    "src_ip",
    "src_ips",
    "source_ip",
    "source_ips",
    "srcip",
    "dst_ip",
    "dst_ips",
    "dest_ip",
    "dest_ips",
    "destination_ip",
    "destination_ips",
    "dstip",
    "ip",
    "ips",
    "ip_address",
    "ip_addresses",
    "local_ip",
    "remote_ip",
    "client_ip",
)

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


def _names_any(entity_context, ips) -> ColumnElement[bool]:
    return func.coalesce(
        or_(*(entity_context[key].op("?|")(ips) for key in FINDING_IP_KEYS)),
        false(),
    )


def finding_names_any(ips) -> ColumnElement[bool]:
    """SQL: the finding names any of ``ips`` (a ``text[]`` expression).

    ``jsonb ?| text[]`` matches a string scalar and the string elements of an
    array, the two shapes :data:`FINDING_IP_KEYS` hold. Coalesced so a finding
    without entity context is false rather than NULL: ``NOT NULL`` would drop it
    from the hidden view as well.
    """
    return _names_any(Finding.entity_context, ips)


def exclusion_view_filter(view: str) -> Optional[ColumnElement[bool]]:
    """WHERE clause for one of :data:`EXCLUSION_VIEWS`; ``None`` means no filter."""
    if view not in EXCLUSION_VIEWS:
        raise ValueError(f"exclusions must be one of {EXCLUSION_VIEWS}; got {view!r}")
    if view == "include":
        return None
    # CASE, not OR: Postgres may evaluate both arms of OR, and the ``?|`` test
    # is what makes an empty exclusion list expensive. The skipped arm is not run.
    none_active = func.cardinality(active_ips_subquery()) == 0
    matches = finding_names_any(active_ips_subquery())
    if view == "only":
        return case((none_active, false()), else_=matches)
    return case((none_active, true()), else_=~matches)


def count_findings_per_active_ip(session: Session) -> Tuple[Dict[str, int], int]:
    """``({address: findings naming it}, findings naming any)`` in one scan:
    the per-address join only sees findings that already name an exclusion.
    ROLLUP adds the total as the row whose address is NULL. Nothing active means
    nothing is hidden, and the findings scan is skipped."""
    if (
        session.execute(
            select(IpExclusion.ip).where(IpExclusion.removed_at.is_(None)).limit(1)
        ).first()
        is None
    ):
        return {}, 0
    hidden = (
        select(Finding.finding_id, Finding.entity_context)
        .where(finding_names_any(active_ips_subquery()))
        .subquery()
    )
    active = (
        select(IpExclusion.ip.label("ip"))
        .where(IpExclusion.removed_at.is_(None))
        .subquery()
    )
    rows = session.execute(
        select(active.c.ip, func.count(distinct(hidden.c.finding_id)))
        .select_from(
            hidden.join(
                active,
                _names_any(hidden.c.entity_context, array([cast(active.c.ip, Text)])),
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
