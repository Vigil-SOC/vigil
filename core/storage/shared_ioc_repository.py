"""Repository for ``shared_iocs`` — the cross-investigation IOC index.

An indicator is keyed as ``"<ioc_type>:<value>"``, stripped and lower-cased.
Operates on a caller-provided ``Session``; it never opens or closes one.
"""

import logging
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from core.memory.entity_keys import entity_context_candidates
from core.storage.models import (
    Case,
    CaseIOC,
    Finding,
    Investigation,
    SharedIOC,
    case_findings,
)

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


class CaseMatch(NamedTuple):
    """A Case a finding belongs with, and the key that says so."""

    case_id: str
    key: str  # ``ip:…``, ``hash:…`` or ``hostname:…``
    same_alert: bool = False  # a hostname match, backed by the same data_source + title


# Listed in the order a match is preferred when one Case matches several ways.
_KEY_RANK = {"ip": 0, "hash": 1, "hostname": 2}


def finding_match_keys(finding: Dict[str, Any]) -> Tuple[Set[str], Set[str]]:
    """The keys a finding can join a Case on: ``(ip and hash keys, hostname keys)``.

    Source IPs come from ``src_ip``/``src_ips`` alone; ``entity_context_candidates``
    yields destination IPs as ``ip`` too, and an attacker is the source.
    """
    context = finding.get("entity_context")
    context = context if isinstance(context, dict) else {}
    sources = list(context.get("src_ips") or []) + [context.get("src_ip")]
    entity = {make_key("ip", str(v)) for v in sources if v}
    hosts = set()
    for kind, value in entity_context_candidates(finding):
        if kind == "hash":
            entity.add(make_key("hash", value))
        elif kind == "host":
            hosts.add(make_key("hostname", value))
    entity.discard(None)
    hosts.discard(None)
    return entity, hosts


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

    def matching_cases(self, finding: Dict[str, Any], *, open: bool) -> List[CaseMatch]:
        """Open (or closed) Cases this finding belongs with, newest ``updated_at`` first.

        A Case matches on a source IP or file hash shared with one of its
        investigations, or on a hostname shared with one of them while the Case
        holds a finding with the same ``data_source`` and trimmed, case-insensitive
        ``title``. Raises on a failed read: the caller decides what that means.
        """
        entity_keys, host_keys = finding_match_keys(finding)
        title = (finding.get("title") or "").strip().lower()
        source = finding.get("data_source")
        clauses = self._key_clauses(entity_keys)
        host_clauses = (
            self._key_clauses(host_keys)
            if title and source
            else []  # no title, no rule
        )
        if not clauses and not host_clauses:
            return []

        status = Case.status != "closed" if open else Case.status == "closed"
        base = (
            select(Case.case_id, SharedIOC.ioc_type, SharedIOC.value, Case.updated_at)
            .join(Investigation, Investigation.case_id == Case.case_id)
            .join(
                SharedIOC, SharedIOC.investigation_id == Investigation.investigation_id
            )
            .where(status)
        )
        rows: List[Tuple[str, str, str, Any, bool]] = []
        if clauses:
            rows += [
                (*r, False) for r in self.session.execute(base.where(or_(*clauses)))
            ]
        if host_clauses:
            same_title = (
                base.join(case_findings, case_findings.c.case_id == Case.case_id)
                .join(Finding, Finding.finding_id == case_findings.c.finding_id)
                .where(
                    or_(*host_clauses),
                    Finding.data_source == source,
                    func.lower(func.btrim(Finding.title, " \t\r\n")) == title,
                )
            )
            rows += [(*r, True) for r in self.session.execute(same_title)]

        rows.sort(key=lambda r: (-r[3].timestamp(), _KEY_RANK.get(r[1], 9)))
        seen: Set[str] = set()
        matches: List[CaseMatch] = []
        for case_id, ioc_type, value, _, same_alert in rows:
            if case_id not in seen:
                seen.add(case_id)
                matches.append(CaseMatch(case_id, f"{ioc_type}:{value}", same_alert))
        return matches

    def record_on_case(self, case_id: str, keys: Iterable[str]) -> int:
        """Index ``keys`` against the Case's newest investigation. 0 if it has none."""
        investigation_id = self.session.execute(
            select(Investigation.investigation_id)
            .where(Investigation.case_id == case_id)
            .order_by(Investigation.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return self.record(investigation_id, keys) if investigation_id else 0

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
