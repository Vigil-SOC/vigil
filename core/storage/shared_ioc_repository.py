"""Repository for ``shared_iocs`` — the index intake joins alerts to Cases by.

A Case is the subject: its rows are kept in step with what it holds by
``reconcile_case``. An indicator is keyed as ``"<ioc_type>:<value>"``, stripped,
defanged and lower-cased — the rule memory's Entity Keys use, so the two agree.
Operates on a caller-provided ``Session``; it never opens or closes one.
"""

import logging
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core.storage.case_indicators import (
    LINK_ONLY_TYPES,
    case_indicators,
    context_indicators,
    intake_use,
)
from core.storage.defang import defang, trim_edges
from core.storage.models import (
    Case,
    CaseIOC,
    Finding,
    SharedIOC,
    case_findings,
)
from core.time import utcnow

logger = logging.getLogger(__name__)

# Findings yield these five types; case_iocs.ioc_type is free text and spells
# several of them differently. Folding the aliases is what lets a row written
# on case close join a key written at investigation start.
TYPE_ALIASES = {
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

# An IP's role rides in its ioc_type (no new column). A key with no role -- ``ip``,
# written before roles or from an IOC typed plain ``ip`` -- never attaches an alert.
ROLE_TYPES = {"src_ip": "src_ip", "dst_ip": "dst_ip", "dest_ip": "dst_ip"}

_MAX_TYPE_LEN = 30
_MAX_VALUE_LEN = 500


def make_key(ioc_type: Optional[str], value: Optional[str]) -> Optional[str]:
    ioc_type = (ioc_type or "").strip().lower()
    ioc_type = ROLE_TYPES.get(ioc_type) or TYPE_ALIASES.get(ioc_type, ioc_type)
    value = defang(trim_edges(value or "")).lower()
    if not ioc_type or not value:
        return None
    # Dropped rather than truncated: a truncated indicator joins against
    # unrelated ones.
    if len(ioc_type) > _MAX_TYPE_LEN or len(value) > _MAX_VALUE_LEN:
        logger.debug("Dropping oversized IOC key %s:%.40s...", ioc_type, value)
        return None
    return f"{ioc_type}:{value}"


def fold_key(key: Optional[str]) -> Optional[str]:
    """An IP key with its role folded to ``ip``; any other key unchanged."""
    for role in ("src_ip:", "dst_ip:"):
        if key and key.startswith(role):
            return "ip:" + key[len(role) :]
    return key


def mark_key(ioc_type: Optional[str], value: Optional[str]) -> Optional[str]:
    """``make_key`` with the IP role folded: what a false-positive mark is compared by."""
    return fold_key(make_key(ioc_type, value))


def split_key(key: str) -> Optional[Tuple[str, str]]:
    # First colon only — an IPv6 address is all colons after that.
    ioc_type, sep, value = (key or "").partition(":")
    if not sep or not ioc_type or not value:
        return None
    return ioc_type, value


def marked_keys(session: Session, case_ids: Iterable[str]) -> Dict[str, Set[str]]:
    """Each Case's keys a person marked a false positive, by ``mark_key``.

    A mark covers every role of an IP (``src_ip``, ``dst_ip``, ``ip``), so the
    sets hold role-folded keys; compare with ``mark_key``/``fold_key``. The one reading of the mark: matching honours it here, and recording a
    value again looks it up here, so the two cannot drift.
    """
    ids = set(case_ids)
    out: Dict[str, Set[str]] = {}
    if not ids:
        return out
    rows = session.execute(
        select(CaseIOC.case_id, CaseIOC.ioc_type, CaseIOC.value).where(
            CaseIOC.case_id.in_(ids), CaseIOC.is_false_positive.is_(True)
        )
    )
    for case_id, ioc_type, value in rows:
        if key := mark_key(ioc_type, value):
            out.setdefault(case_id, set()).add(key)
    return out


def reconcile_case_safely(
    session: Session, case_id: str, changed: Optional[Iterable[str]] = None
) -> bool:
    """Reconcile a Case's rows without letting a failure fail the caller.

    ``changed`` is the ``ioc_type`` of what the caller changed, when it knows;
    unset means anything. A failure is logged and the Case marked stale, only if
    what changed has a type intake can use.
    """
    # Flush first so the SAVEPOINT covers only our writes and rolling it back
    # cannot undo the caller's change; a failure here is the caller failing.
    # Without the SAVEPOINT, catching on a shared session poisons the transaction
    # and the caller's commit raises anyway.
    session.flush()
    try:
        with session.begin_nested():
            return SharedIOCRepository(session).reconcile_case(case_id)
    except Exception as e:
        logger.error("Failed to index IOCs for %s: %s", case_id, e, exc_info=True)
    if changed is None or any(
        intake_use((make_key(t, "x") or ":").partition(":")[0]) for t in changed
    ):
        try:
            with session.begin_nested():
                session.execute(
                    update(Case).where(
                        Case.case_id == case_id, Case.indicators_stale_since.is_(None)
                    )
                    # Not an edit of the Case: leave updated_at alone.
                    .values(indicators_stale_since=utcnow(), updated_at=Case.updated_at)
                )
        except Exception as e:
            logger.error("Failed to mark %s stale: %s", case_id, e)
    return False


class CaseMatch(NamedTuple):
    """A Case a finding belongs with, and the key that says so."""

    case_id: str
    key: str  # ``src_ip:…``, ``dst_ip:…``, ``ip:…``, ``hash:…`` or ``hostname:…``
    same_alert: bool = False  # a hostname match, backed by the same data_source + title

    @property
    def attaches(self) -> bool:
        return self.key.partition(":")[0] not in LINK_ONLY_TYPES


# Listed in the order a match is preferred when one Case matches several ways:
# the ways that attach first, so a Case matching both ways attaches.
_KEY_RANK = {"src_ip": 0, "hash": 1, "hostname": 2, "dst_ip": 3, "ip": 4}


def finding_match_keys(finding: Dict[str, Any]) -> Tuple[Set[str], Set[str]]:
    """The keys a finding can join a Case on: ``(ip and hash keys, hostname keys)``.

    An attacker is the source, so only source IPs are looked up, under every type
    an IP is stored as: ``src_ip`` attaches, ``dst_ip`` and role-less ``ip`` link.
    """
    entity: Set[Optional[str]] = set()
    hosts: Set[Optional[str]] = set()
    for ioc_type, value in context_indicators(finding.get("entity_context")):
        if ioc_type == "src_ip":
            entity |= {make_key(t, value) for t in ("src_ip", *LINK_ONLY_TYPES)}
        elif ioc_type == "hash":
            entity.add(make_key(ioc_type, value))
        elif ioc_type == "host":
            hosts.add(make_key("hostname", value))
    return entity - {None}, hosts - {None}


class SharedIOCRepository:
    """Data access for the cross-investigation IOC index."""

    def __init__(self, session: Session):
        self.session = session

    # ---- writes --------------------------------------------------------

    def reconcile_case(self, case_id: str) -> bool:
        """Make a Case's rows exactly what it holds now. False if there is no such Case.

        Locks the Case row. Keys a person marked a false positive (by the mark
        fold) are left out, and only the types intake reads are written. Stamps
        the Case synced and clears its stale mark.
        """
        if (
            self.session.scalar(
                select(Case.case_id).where(Case.case_id == case_id).with_for_update()
            )
            is None
        ):
            return False

        marked = marked_keys(self.session, [case_id]).get(case_id, set())
        wanted: Set[Tuple[str, str]] = set()
        for ioc_type, value in case_indicators(self.session, case_id):
            key = make_key(ioc_type, value)
            pair = split_key(key) if key else None
            if pair and intake_use(pair[0]) and fold_key(key) not in marked:
                wanted.add(pair)

        have = {
            (t, v)
            for t, v in self.session.execute(
                select(SharedIOC.ioc_type, SharedIOC.value).where(
                    SharedIOC.case_id == case_id
                )
            )
        }
        if wanted - have:
            self.session.execute(
                insert(SharedIOC)
                .values(
                    [
                        {"case_id": case_id, "ioc_type": t, "value": v}
                        for t, v in sorted(wanted - have)
                    ]
                )
                .on_conflict_do_nothing(index_elements=["case_id", "ioc_type", "value"])
            )
        if have - wanted:
            self.session.execute(
                delete(SharedIOC).where(
                    SharedIOC.case_id == case_id,
                    tuple_(SharedIOC.ioc_type, SharedIOC.value).in_(have - wanted),
                )
            )
        self.session.execute(
            update(Case)
            .where(Case.case_id == case_id)
            # Not an edit of the Case: leave updated_at alone.
            .values(
                indicators_synced_at=utcnow(),
                indicators_stale_since=None,
                updated_at=Case.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        return True

    # ---- reads ---------------------------------------------------------

    def matching_cases(self, finding: Dict[str, Any], *, open: bool) -> List[CaseMatch]:
        """Open (or closed) Cases this finding belongs with, newest ``updated_at`` first.

        A Case matches on a source IP or file hash it holds (an IP that was only a
        destination, or whose role was never recorded, matches too but doesn't
        ``attach``), or on a hostname it holds while the Case has a finding with
        the same ``data_source`` and trimmed, case-insensitive ``title``.
        Raises on a failed read: the caller decides what that means.
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
        columns = (Case.case_id, SharedIOC.ioc_type, SharedIOC.value, Case.updated_at)
        base = (
            select(*columns)
            .join(SharedIOC, SharedIOC.case_id == Case.case_id)
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

        # A key marked false positive on a Case no longer matches it; its other
        # keys still do. Read here, not at index write, so it holds at once.
        marked = marked_keys(self.session, {r[0] for r in rows})
        rows = [
            r for r in rows if fold_key(f"{r[1]}:{r[2]}") not in marked.get(r[0], ())
        ]

        rows.sort(key=lambda r: (-r[3].timestamp(), _KEY_RANK.get(r[1], 9)))
        seen: Set[str] = set()
        matches: List[CaseMatch] = []
        for case_id, ioc_type, value, _, same_alert in rows:
            if case_id not in seen:
                seen.add(case_id)
                matches.append(CaseMatch(case_id, f"{ioc_type}:{value}", same_alert))
        return matches

    # ---- internals -----------------------------------------------------

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
