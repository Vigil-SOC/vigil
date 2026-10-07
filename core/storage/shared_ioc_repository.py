"""Repository for ``shared_iocs`` — the cross-investigation IOC index.

An indicator is keyed as ``"<ioc_type>:<value>"``, stripped and lower-cased.
Operates on a caller-provided ``Session``; it never opens or closes one.
"""

import logging
from datetime import datetime
from typing import Iterable, List, Optional, Set, Tuple

from sqlalchemy import ColumnElement, DateTime, and_, func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from core.storage.models import CaseIOC, Investigation, SharedIOC
from core.time import utcnow

logger = logging.getLogger(__name__)

CLOSED_STATUSES = ("completed", "failed")

# Findings yield these five types; case_iocs.ioc_type is free text and spells
# several of them differently. Folding the aliases is what lets a row written
# on case close join a key written at investigation start.
_TYPE_ALIASES = {
    "filehash": "hash",
    "file_hash": "hash",
    "md5": "hash",
    "sha1": "hash",
    "sha256": "hash",
    "sha512": "hash",
    "ipaddress": "ip",
    "ip_address": "ip",
    "ipv4": "ip",
    "ipv6": "ip",
    "src_ip": "ip",
    "dst_ip": "ip",
    "dest_ip": "ip",
    "domain_name": "domain",
    "fqdn": "domain",
    "host": "hostname",
    "account": "user",
    "user_name": "user",
    "username": "user",
}

_MAX_TYPE_LEN = 30
_MAX_VALUE_LEN = 500


def make_key(ioc_type: Optional[str], value: Optional[str]) -> Optional[str]:
    ioc_type = (ioc_type or "").strip().lower()
    ioc_type = _TYPE_ALIASES.get(ioc_type, ioc_type)
    value = (value or "").strip().lower()
    if not ioc_type or not value:
        return None
    # Dropped rather than truncated: a truncated indicator joins against
    # unrelated ones.
    if len(ioc_type) > _MAX_TYPE_LEN or len(value) > _MAX_VALUE_LEN:
        logger.debug("Dropping oversized IOC key %s:%.40s...", ioc_type, value)
        return None
    return f"{ioc_type}:{value}"


def split_key(key: str) -> Optional[Tuple[str, str]]:
    # First colon only — an IPv6 address is all colons after that.
    ioc_type, sep, value = (key or "").partition(":")
    if not sep or not ioc_type or not value:
        return None
    return ioc_type, value


def index_case_iocs_on_close(session: Session, case_id: str) -> int:
    """Index a closing case's IOCs without letting a failure fail the close."""
    # Flush first so the SAVEPOINT covers only our inserts and rolling it back
    # cannot undo the close; a failure here is the close failing. Without the
    # SAVEPOINT, catching on a shared session poisons the transaction and the
    # caller's commit raises anyway.
    session.flush()
    try:
        with session.begin_nested():
            return SharedIOCRepository(session).record_case(case_id)
    except Exception as e:
        logger.error(
            "Failed to index shared IOCs for %s: %s", case_id, e, exc_info=True
        )
        return 0


def _live_clause(now: datetime) -> ColumnElement[bool]:
    """An investigation still worth deduplicating a new finding against.

    Status alone is not enough: ``needs_rework`` nobody reworks, or
    ``executing`` orphaned by a crash, would stay live forever and silence
    every future finding on its indicators. Anything untouched for longer than
    its own runtime ceiling is stale, so crashed runs age out on their own.
    """
    last_seen = func.coalesce(
        Investigation.last_activity_at,
        Investigation.started_at,
        Investigation.created_at,
    )
    age_seconds = func.extract("epoch", literal(now, DateTime) - last_seen)
    return and_(
        Investigation.status.notin_(CLOSED_STATUSES),
        age_seconds < Investigation.max_runtime_seconds,
    )


class SharedIOCRepository:
    """Data access for the cross-investigation IOC index."""

    def __init__(self, session: Session):
        self.session = session

    # ---- writes --------------------------------------------------------

    def record(self, investigation_id: str, keys: Iterable[str]) -> int:
        """Index ``keys``, skipping ones already there. Returns rows added."""
        pairs = {p for p in (split_key(k) for k in keys) if p is not None}
        if not pairs:
            return 0

        new = pairs - self._pairs_for(investigation_id)
        for ioc_type, value in sorted(new):
            self.session.add(
                SharedIOC(
                    investigation_id=investigation_id,
                    ioc_type=ioc_type,
                    value=value,
                )
            )
        return len(new)

    def record_case(self, case_id: str) -> int:
        """Index a case's IOCs against every investigation that belongs to it.

        A case with no investigation contributes nothing — ``investigation_id``
        is the table's only subject. Attribution is at case granularity.
        """
        keys = self._case_keys(case_id)
        if not keys:
            return 0

        investigation_ids = (
            self.session.execute(
                select(Investigation.investigation_id).where(
                    Investigation.case_id == case_id
                )
            )
            .scalars()
            .all()
        )
        return sum(self.record(inv_id, keys) for inv_id in investigation_ids)

    def record_case_for(self, case_id: str, investigation_id: str) -> int:
        return self.record(investigation_id, self._case_keys(case_id))

    # ---- reads ---------------------------------------------------------

    def keys_for(self, investigation_id: str) -> Set[str]:
        return {f"{t}:{v}" for t, v in self._pairs_for(investigation_id)}

    def investigations_for(self, keys: Iterable[str]) -> Set[str]:
        """Investigations that have seen any of ``keys``, open or finished."""
        return self._investigations(keys, live_only=False)

    def open_investigations_for(self, keys: Iterable[str]) -> Set[str]:
        """As ``investigations_for``, restricted to investigations still live."""
        return self._investigations(keys, live_only=True)

    def shared_between(self, inv_id_a: str, inv_id_b: str) -> Set[str]:
        other = aliased(SharedIOC)
        rows = self.session.execute(
            select(SharedIOC.ioc_type, SharedIOC.value)
            .join(
                other,
                and_(
                    other.ioc_type == SharedIOC.ioc_type,
                    other.value == SharedIOC.value,
                ),
            )
            .where(
                SharedIOC.investigation_id == inv_id_a,
                other.investigation_id == inv_id_b,
            )
            .distinct()
        ).all()
        return {f"{t}:{v}" for t, v in rows}

    # ---- internals -----------------------------------------------------

    def _investigations(self, keys: Iterable[str], *, live_only: bool) -> Set[str]:
        clauses = self._key_clauses(keys)
        if not clauses:
            return set()
        stmt = select(SharedIOC.investigation_id).where(or_(*clauses))
        if live_only:
            stmt = stmt.join(
                Investigation,
                Investigation.investigation_id == SharedIOC.investigation_id,
            ).where(_live_clause(utcnow()))
        return set(self.session.execute(stmt.distinct()).scalars().all())

    def _pairs_for(self, investigation_id: str) -> Set[Tuple[str, str]]:
        rows = self.session.execute(
            select(SharedIOC.ioc_type, SharedIOC.value).where(
                SharedIOC.investigation_id == investigation_id
            )
        ).all()
        return {(t, v) for t, v in rows}

    def _case_keys(self, case_id: str) -> Set[str]:
        values = self.session.execute(
            select(CaseIOC.ioc_type, CaseIOC.value).where(CaseIOC.case_id == case_id)
        ).all()
        return {k for k in (make_key(t, v) for t, v in values) if k}

    @staticmethod
    def _key_clauses(keys: Iterable[str]) -> List[ColumnElement[bool]]:
        clauses: List[ColumnElement[bool]] = []
        for key in keys:
            pair = split_key(key)
            if pair is None:
                continue
            clauses.append(
                and_(SharedIOC.ioc_type == pair[0], SharedIOC.value == pair[1])
            )
        return clauses
