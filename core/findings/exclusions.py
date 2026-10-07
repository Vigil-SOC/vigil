"""Analyst IP exclusions: which findings the console queue hides, and which a
hunt or investigation may not start from.

An exclusion never touches a finding. Ingest does not read the table, so LogLM
keeps scoring and storing findings for an excluded address with their original
severity and status; removing the exclusion shows them again unchanged.
Exclusions are org-wide: a Vigil deployment is one organisation, and every
analyst's queue hides the same addresses.

A finding is *excluded* when any address among its top-level entity IP fields
(``FINDING_IP_KEYS``) is actively excluded. Any, not all: the addresses an
analyst excludes are known-bad externals (scanners, sinkholed C2) whose every
finding also names one of our own hosts, so an all-addresses rule would never
hide anything. Addresses inside bounded source-evidence records are not
consulted -- a netflow preview names every peer in the window, and matching
those would hide findings the analyst never looked at.

Rows and the SQL predicate are in ``core.storage.ip_exclusion_repository``.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.findings.ip_address import normalize_ip
from core.storage import ip_exclusion_repository as repo
from core.storage.ip_exclusion_repository import EXCLUSION_VIEWS, FINDING_IP_KEYS
from core.storage.models import IpExclusion
from core.storage.models.exclusion import EXCLUSION_ORIGINS
from core.storage.schemas.base import _iso_utc
from core.time import utcnow

__all__ = [
    "EXCLUSION_VIEWS",
    "FINDING_IP_KEYS",
    "ExclusionConflict",
    "ExclusionError",
    "cached_active_ips",
    "create_exclusion",
    "current_active_ips",
    "excluded_ips_of",
    "finding_ips",
    "hidden_findings_total",
    "invalidate_cache",
    "list_exclusions",
    "list_exclusions_with_total",
    "normalize_ip",
    "remove_exclusion",
]

logger = logging.getLogger(__name__)

MAX_REASON_LENGTH = 2000


class ExclusionError(ValueError):
    """A request the exclusion store refuses; the message is user-facing."""


class ExclusionConflict(ExclusionError):
    """The address is already actively excluded."""


def finding_ips(entity_context: Any) -> FrozenSet[str]:
    """Every normalized address a finding names in its entity IP fields."""
    if not isinstance(entity_context, Mapping):
        return frozenset()
    found = set()
    for key in FINDING_IP_KEYS:
        value = entity_context.get(key)
        for item in value if isinstance(value, (list, tuple)) else [value]:
            ip = normalize_ip(item)
            if ip:
                found.add(ip)
    return frozenset(found)


def excluded_ips_of(finding: Mapping[str, Any], active: Iterable[str]) -> List[str]:
    """The finding's addresses that are actively excluded, sorted."""
    active_set = active if isinstance(active, (set, frozenset)) else set(active)
    return sorted(finding_ips(finding.get("entity_context")) & active_set)


def serialize(
    row: IpExclusion, hidden_findings: Optional[int] = None
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "exclusion_id": row.exclusion_id,
        "ip": row.ip,
        "reason": row.reason,
        "origin": row.origin,
        "origin_ref": row.origin_ref,
        "created_by": row.created_by,
        "created_at": _iso_utc(row.created_at) if row.created_at else None,
        "removed_at": _iso_utc(row.removed_at) if row.removed_at else None,
        "removed_by": row.removed_by,
        "removal_reason": row.removal_reason,
        "active": row.removed_at is None,
    }
    if hidden_findings is not None:
        data["hidden_findings"] = hidden_findings
    return data


def list_exclusions_with_total(
    session: Session, *, include_removed: bool = False
) -> Tuple[List[Dict[str, Any]], int]:
    """:func:`list_exclusions` and :func:`hidden_findings_total` from one pass
    over the findings table."""
    per_ip, total = repo.count_findings_per_active_ip(session)
    rows = [
        serialize(row, per_ip.get(row.ip, 0) if row.removed_at is None else None)
        for row in repo.list_rows(session, include_removed=include_removed)
    ]
    return rows, total


def list_exclusions(
    session: Session, *, include_removed: bool = False, with_counts: bool = True
) -> List[Dict[str, Any]]:
    """Newest first. Active rows carry ``hidden_findings``: how many stored
    findings name the address, which is what removing it would bring back."""
    if not with_counts:
        return [
            serialize(row)
            for row in repo.list_rows(session, include_removed=include_removed)
        ]
    return list_exclusions_with_total(session, include_removed=include_removed)[0]


def hidden_findings_total(session: Session) -> int:
    """Findings the queue is hiding, each counted once -- the per-row counts
    overlap when one finding names two excluded addresses."""
    return repo.count_findings_per_active_ip(session)[1]


def _clean_reason(reason: Any, *, required: bool) -> Optional[str]:
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        if required:
            raise ExclusionError("A reason is required")
        return None
    if len(text) > MAX_REASON_LENGTH:
        raise ExclusionError(f"Reason must be at most {MAX_REASON_LENGTH} characters")
    return text


def create_exclusion(
    session: Session,
    *,
    ip: Any,
    reason: Any,
    created_by: str,
    origin: str = "ad_hoc",
    origin_ref: Optional[str] = None,
) -> Dict[str, Any]:
    normalized = normalize_ip(ip)
    if normalized is None:
        raise ExclusionError(f"{ip!r} is not a single IPv4 or IPv6 address")
    if origin not in EXCLUSION_ORIGINS:
        raise ExclusionError(f"origin must be one of {', '.join(EXCLUSION_ORIGINS)}")
    clean_reason = _clean_reason(reason, required=True)
    if repo.active_row_for(session, normalized) is not None:
        raise ExclusionConflict(f"{normalized} is already excluded")
    row = IpExclusion(
        exclusion_id=f"excl-{uuid.uuid4().hex[:16]}",
        ip=normalized,
        reason=clean_reason,
        origin=origin,
        origin_ref=str(origin_ref)[:100] if origin_ref else None,
        created_by=created_by,
        created_at=utcnow(),
    )
    # Two analysts excluding the same address at once both pass the check above;
    # the SAVEPOINT keeps the unique index's refusal from poisoning the request.
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as e:
        constraint = getattr(getattr(e.orig, "diag", None), "constraint_name", None)
        if constraint != "uniq_ip_exclusions_active_ip":
            raise
        raise ExclusionConflict(f"{normalized} is already excluded") from e
    _invalidate_on_commit(session)
    logger.info(
        "IP exclusion %s added for %s by %s", row.exclusion_id, normalized, created_by
    )
    return serialize(row)


def remove_exclusion(
    session: Session, exclusion_id: str, *, removed_by: str, reason: Any = None
) -> Optional[Dict[str, Any]]:
    """Mark an active exclusion removed; ``None`` when there is no such row.

    Removing an already-removed exclusion returns it unchanged, so a double
    click is not an error and the first removal's record is kept.
    """
    row = session.get(IpExclusion, exclusion_id)
    if row is None:
        return None
    if row.removed_at is None:
        row.removed_at = utcnow()
        row.removed_by = removed_by
        row.removal_reason = _clean_reason(reason, required=False)
        session.flush()
        _invalidate_on_commit(session)
        logger.info(
            "IP exclusion %s for %s removed by %s", exclusion_id, row.ip, removed_by
        )
    return serialize(row)


# Agent seed selection asks per run; a short TTL keeps that from being a query
# per finding while a change still lands within seconds. Writes made through
# this module drop it at once (in this process; others wait out the TTL).
_CACHE_TTL_SECONDS = 15.0
_cache_lock = threading.Lock()
_cache: Optional[FrozenSet[str]] = None
_cache_at = 0.0


def invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def _invalidate_on_commit(session: Session) -> None:
    # Now and again after commit: a reader between the two would otherwise
    # cache the pre-commit set for a full TTL.
    invalidate_cache()
    event.listen(session, "after_commit", lambda _s: invalidate_cache(), once=True)


def _read_active_ips() -> FrozenSet[str]:
    from core.storage.unit_of_work import unit_of_work

    with unit_of_work() as session:
        return frozenset(repo.active_ips(session))


def _fail_open(e: Exception) -> FrozenSet[str]:
    # An exclusion hides findings, so failing closed would hide everything.
    logger.warning("Could not read IP exclusions; treating none as active: %s", e)
    return frozenset()


def current_active_ips() -> FrozenSet[str]:
    """Uncached: the findings filter reads the table live, and with more than
    one backend replica a cached label would disagree with it for a TTL."""
    try:
        return _read_active_ips()
    except Exception as e:  # noqa: BLE001
        return _fail_open(e)


def cached_active_ips() -> FrozenSet[str]:
    """:func:`current_active_ips` behind a short TTL. A failed read is not
    cached."""
    global _cache, _cache_at
    with _cache_lock:
        if _cache is not None and time.monotonic() - _cache_at < _CACHE_TTL_SECONDS:
            return _cache
    try:
        value = _read_active_ips()
    except Exception as e:  # noqa: BLE001
        return _fail_open(e)
    with _cache_lock:
        _cache, _cache_at = value, time.monotonic()
    return value
