"""Repository for ``shared_iocs`` — the cross-investigation IOC index.

An indicator is keyed as ``"<ioc_type>:<value>"``, stripped and lower-cased.
Operates on a caller-provided ``Session``; it never opens or closes one.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from core.storage.models import Case, CaseIOC, Finding, Investigation, SharedIOC
from core.storage.models.base import case_findings

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class CaseMatch:
    """A Case that a finding matches, and the key it matched on.

    ``same_alert`` marks a hostname match, which counts only because the Case
    also holds a finding with the same ``data_source`` and title.
    """

    case_id: str
    key: str
    same_alert: bool = False


def _normal_title(title: Any) -> str:
    return str(title or "").strip().lower()


def _src_ip_keys(context: Dict[str, Any]) -> Set[str]:
    listed = context.get("src_ips") or []
    values = list(listed) if isinstance(listed, (list, tuple)) else []
    if context.get("src_ip"):
        values.append(context["src_ip"])
    return {k for k in (make_key("ip", str(v)) for v in values) if k}


def match_keys(
    finding: Dict[str, Any], candidates: Iterable[Tuple[str, str]]
) -> Tuple[Set[str], Set[str]]:
    """``(strong, host)`` keys a finding can match a Case on.

    ``candidates`` are the finding's ``entity_context_candidates``, passed in
    because storage may not import memory. Source IPs are read off the context
    instead: candidates spell destination IPs as ``ip`` too. Strong keys
    (source IPs, file hashes) match on their own; host keys only alongside the
    same alert. Destination IPs, users and domains never match.
    """
    context = finding.get("entity_context")
    if not isinstance(context, dict):
        return set(), set()
    strong = _src_ip_keys(context)
    host: Set[str] = set()
    for kind, value in candidates:
        if kind not in ("hash", "host"):
            continue
        key = make_key(kind, value)
        if key:
            (strong if kind == "hash" else host).add(key)
    return strong, host


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

    def newest_investigation_of(self, case_id: str) -> Optional[str]:
        return self.session.execute(
            select(Investigation.investigation_id)
            .where(Investigation.case_id == case_id)
            .order_by(Investigation.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def matching_cases(
        self,
        finding: Dict[str, Any],
        candidates: Iterable[Tuple[str, str]],
        *,
        open: bool,
        limit: Optional[int] = None,
        exclude_case_id: Optional[str] = None,
    ) -> List[CaseMatch]:
        """Open (or closed) Cases this finding belongs with, newest ``updated_at`` first.

        A Case matches on a shared source IP or file hash, or on a shared
        hostname when it also holds a finding with this finding's
        ``data_source`` and trimmed, case-insensitive ``title``. This is the
        one place that rule lives.
        """
        strong, host = match_keys(finding, candidates)
        title = _normal_title(finding.get("title"))
        data_source = finding.get("data_source")
        if not (title and data_source):
            host = set()
        if not strong and not host:
            return []

        status = Case.status != "closed" if open else Case.status == "closed"
        base = (
            select(Case.case_id, Case.updated_at, SharedIOC.ioc_type, SharedIOC.value)
            .join(Investigation, Investigation.case_id == Case.case_id)
            .join(
                SharedIOC, SharedIOC.investigation_id == Investigation.investigation_id
            )
            .where(status)
        )
        if exclude_case_id:
            base = base.where(Case.case_id != exclude_case_id)

        # case_id -> (updated_at, match); a strong match beats a same-alert one.
        found: Dict[str, Tuple[datetime, CaseMatch]] = {}
        if strong:
            rows = self.session.execute(
                base.where(or_(*self._key_clauses(strong)))
            ).all()
            for case_id, updated_at, t, v in sorted(rows, key=lambda r: (r[2], r[3])):
                found.setdefault(case_id, (updated_at, CaseMatch(case_id, f"{t}:{v}")))
        if host:
            same_alert = (
                select(case_findings.c.case_id)
                .join(Finding, Finding.finding_id == case_findings.c.finding_id)
                .where(
                    Finding.data_source == data_source,
                    func.lower(func.trim(Finding.title)) == title,
                )
            )
            rows = self.session.execute(
                base.where(or_(*self._key_clauses(host)), Case.case_id.in_(same_alert))
            ).all()
            for case_id, updated_at, t, v in sorted(rows, key=lambda r: (r[2], r[3])):
                found.setdefault(
                    case_id, (updated_at, CaseMatch(case_id, f"{t}:{v}", True))
                )

        ordered = sorted(found.values(), key=lambda m: m[0], reverse=True)
        return [m for _, m in ordered][:limit]

    # ---- internals -----------------------------------------------------

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
