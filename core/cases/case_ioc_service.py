"""
Case IOC Service - Indicator of Compromise management.

Handles IOC tracking, enrichment, deduplication, and export.
"""

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from core.cases import case_trail
from core.cases.case_records_service import readable_case
from core.cases.linked_entities import LINKED_SOURCE
from core.exceptions import NotFoundError
from core.storage.case_indicators import context_indicators, intake_use
from core.storage.models import (
    CaseIOC,
    Finding,
    SharedIOC,
    case_findings,
)
from core.storage.schemas import CaseIOCSchema
from core.storage.shared_ioc_repository import (
    fold_key,
    make_key,
    mark_key,
    marked_keys,
    reconcile_case_safely,
    split_key,
)
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)


def is_marked(session: Session, case_id: str, ioc_type: str, value: str) -> bool:
    """Whether the Case holds a false-positive mark on this value, under any spelling or IP role."""
    key = mark_key(ioc_type, value)
    return key is not None and key in marked_keys(session, [case_id]).get(case_id, ())


# The entity list names these, not the IOC list.
_ENTITY_TYPES = ("user", "process")


class CaseIOCService:
    """Service for managing case IOCs."""

    def __init__(self):
        """Initialize the IOC service."""

    def add_ioc(
        self,
        case_id: str,
        ioc_type: str,
        value: str,
        threat_level: Optional[str] = None,
        confidence: Optional[float] = None,
        source: Optional[str] = None,
        first_seen: Optional[datetime] = None,
        last_seen: Optional[datetime] = None,
        tags: Optional[List[str]] = None,
        context: Optional[str] = None,
        session: Optional[Session] = None,
        added_by: Optional[str] = None,
    ) -> Optional[CaseIOC]:
        """
        Add an IOC to a case.

        Args:
            case_id: Case ID
            ioc_type: IOC type (ip, domain, hash, url, email, etc.)
            value: IOC value
            threat_level: Threat level
            confidence: Confidence score (0-1)
            source: Source of IOC
            first_seen: First seen timestamp
            last_seen: Last seen timestamp
            tags: Tags
            context: Additional context
            session: Database session (optional)
            added_by: Signed-in username; unset is the bound caller

        Returns:
            Created CaseIOC or None

        A new IOC is one ``case.ioc`` trail entry in the same transaction; one
        already on the case only moves ``last_seen``. A failed append fails
        the write: an owned session returns None, a joined one raises
        ``TrailWriteError``; ``trail.Paused`` always propagates.
        """
        joined = session is not None
        try:
            with unit_of_work(session) as session:
                readable_case(session, case_id)
                ioc, created = self._add(
                    session,
                    case_id,
                    ioc_type,
                    value,
                    threat_level=threat_level,
                    confidence=confidence,
                    source=source,
                    first_seen=first_seen,
                    last_seen=last_seen,
                    tags=tags,
                    context=context,
                )
                if created:
                    reconcile_case_safely(session, case_id, [ioc_type])
                    # The type is hashed; the value sits beside it, erasable.
                    case_trail.append_or_legacy(
                        session,
                        action=case_trail.IOC,
                        signed_in=added_by,
                        case_id=case_id,
                        operator_text=value,
                        ids=lambda: {"ioc_id": ioc.ioc_id, "ioc_type": ioc_type},
                    )
                return ioc
        except Exception as exc:
            if isinstance(exc, NotFoundError) or case_trail.propagates(exc, joined):
                raise
            logger.error("Error adding IOC: %s", exc)
            return None

    def set_false_positive(
        self,
        case_id: str,
        ioc_type: str,
        value: str,
        marked: bool,
        session: Optional[Session] = None,
        added_by: Optional[str] = None,
    ) -> Tuple[List[CaseIOC], bool]:
        """Mark or clear a value as a false positive on this Case.

        The mark is by folded value (``mark_key``), so it covers every row the
        Case holds for it, whatever the spelling or IP role (``src_ip``,
        ``dst_ip`` and ``ip`` are one value). Marking a value no row holds
        (one a linked alert names) writes the row, flagged. The trail entry is
        written only when something changed. Returns the rows and whether they
        changed. A value that cannot be keyed is a ``ValueError``.
        """
        key = mark_key(ioc_type, value)
        if key is None:
            raise ValueError("ioc_type and value must be set and not oversized")
        with unit_of_work(session) as session:
            readable_case(session, case_id)
            rows = [
                r
                for r in session.query(CaseIOC).filter(CaseIOC.case_id == case_id)
                if mark_key(r.ioc_type, r.value) == key
            ]
            stale = [r for r in rows if bool(r.is_false_positive) != marked]
            for r in stale:
                r.is_false_positive = marked
            if marked and not rows:
                now = utcnow()
                row = CaseIOC(
                    case_id=case_id,
                    ioc_type=ioc_type,
                    value=value,
                    source=LINKED_SOURCE,
                    first_seen=now,
                    last_seen=now,
                    tags=[],
                    is_active=True,
                    is_false_positive=True,
                )
                session.add(row)
                rows, stale = [row], [row]
            if stale:
                reconcile_case_safely(session, case_id, [ioc_type])
                case_trail.append_or_legacy(
                    session,
                    action=(
                        case_trail.IOC_FALSE_POSITIVE
                        if marked
                        else case_trail.IOC_FALSE_POSITIVE_CLEAR
                    ),
                    signed_in=added_by,
                    case_id=case_id,
                    operator_text=value,
                    ids=lambda: {
                        "ioc_ids": [r.ioc_id for r in stale],
                        "ioc_type": ioc_type,
                    },
                )
            return rows, bool(stale)

    def add_iocs(
        self,
        case_id: str,
        iocs: List[Dict],
        session: Session,
        added_by: Optional[str] = None,
    ) -> List[CaseIOC]:
        """Add a batch in the caller's transaction: one ``case.ioc.bulk``
        entry with the count of new IOCs, not one per IOC. Raises on failure."""
        readable_case(session, case_id)
        added: List[CaseIOC] = []
        created: List[CaseIOC] = []
        for ioc_data in iocs:
            ioc, new = self._add(
                session,
                case_id,
                ioc_data.get("ioc_type", "unknown"),
                ioc_data.get("value", ""),
                threat_level=ioc_data.get("threat_level"),
                confidence=ioc_data.get("confidence"),
                source=ioc_data.get("source"),
                first_seen=ioc_data.get("first_seen"),
                last_seen=ioc_data.get("last_seen"),
                tags=ioc_data.get("tags"),
                context=ioc_data.get("context"),
            )
            added.append(ioc)
            if new:
                created.append(ioc)
        if created:
            reconcile_case_safely(session, case_id, [i.ioc_type for i in created])
            case_trail.append_or_legacy(
                session,
                action=case_trail.IOC_BULK,
                signed_in=added_by,
                case_id=case_id,
                operator_text="\n".join(ioc.value for ioc in created),
                ids=lambda: {
                    "count": len(created),
                    "ioc_ids": [ioc.ioc_id for ioc in created],
                    "ioc_types": sorted({ioc.ioc_type for ioc in created}),
                },
            )
        return added

    def bulk_add_iocs(
        self,
        case_id: str,
        iocs: List[Dict],
        session: Optional[Session] = None,
        added_by: Optional[str] = None,
    ) -> Optional[int]:
        """
        Bulk add IOCs to a case, in one transaction.

        Args:
            case_id: Case ID
            iocs: List of IOC dictionaries
            session: Database session (optional)
            added_by: Signed-in username; unset is the bound caller

        Returns:
            Number of IOCs added, or None when the batch was not written
        """
        joined = session is not None
        try:
            with unit_of_work(session) as session:
                return len(self.add_iocs(case_id, iocs, session, added_by))
        except Exception as exc:
            if isinstance(exc, NotFoundError) or case_trail.propagates(exc, joined):
                raise
            logger.error("Error adding IOCs: %s", exc)
            return None

    def _add(
        self,
        session: Session,
        case_id: str,
        ioc_type: str,
        value: str,
        *,
        threat_level: Optional[str],
        confidence: Optional[float],
        source: Optional[str],
        first_seen: Optional[datetime],
        last_seen: Optional[datetime],
        tags: Optional[List[str]],
        context: Optional[str],
    ) -> Tuple[CaseIOC, bool]:
        """The IOC row, and whether it is new. An existing one is not duplicated."""
        existing = (
            session.query(CaseIOC)
            .filter(
                and_(
                    CaseIOC.case_id == case_id,
                    CaseIOC.ioc_type == ioc_type,
                    CaseIOC.value == value,
                )
            )
            .first()
        )

        if existing:
            # Update last_seen if provided
            if last_seen:
                existing.last_seen = last_seen
            logger.info(f"IOC already exists: {value}")
            return existing, False

        ioc = CaseIOC(
            case_id=case_id,
            ioc_type=ioc_type,
            value=value,
            threat_level=threat_level,
            confidence=confidence,
            source=source,
            first_seen=first_seen or utcnow(),
            last_seen=last_seen or utcnow(),
            tags=tags or [],
            context=context,
            is_active=True,
            # A value a person marked stays marked when it is recorded again.
            is_false_positive=is_marked(session, case_id, ioc_type, value),
        )

        session.add(ioc)

        logger.info(f"Added IOC {ioc_type}:{value} to case {case_id}")
        return ioc, True

    def get_case_iocs(
        self,
        case_id: str,
        ioc_type: Optional[str] = None,
        active_only: bool = False,
        session: Optional[Session] = None,
    ) -> List[CaseIOC]:
        """
        Get all IOCs for a case.

        Args:
            case_id: Case ID
            ioc_type: Filter by IOC type
            active_only: Only return active IOCs
            session: Database session (optional)

        Returns:
            List of CaseIOC objects
        """
        with unit_of_work(session) as session:
            readable_case(session, case_id)
            query = session.query(CaseIOC).filter(CaseIOC.case_id == case_id)

            if ioc_type:
                query = query.filter(CaseIOC.ioc_type == ioc_type)

            if active_only:
                query = query.filter(CaseIOC.is_active.is_(True))

            return query.order_by(CaseIOC.threat_level.desc()).all()

    def list_iocs(
        self,
        case_id: str,
        ioc_type: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> List[dict]:
        """The Case's indicators as the API and MCP list them: recorded, then derived.

        Derived rows are what its findings name, from the one extractor, and carry
        no ``ioc_id``. Each row says what intake does with it (``intake``) and,
        when intake can use it, whether the index holds it yet (``linked``);
        a marked one is never "not linked". Computed on read; nothing is stored.
        """
        with unit_of_work(session) as session:
            readable_case(session, case_id)  # 404 unless the caller may read it
            recorded = self.get_case_iocs(case_id, session=session)
            rows = [(CaseIOCSchema.dump(i), i.ioc_type, i.value) for i in recorded]

            seen = {make_key(t, v) or f"{t}:{v}" for _, t, v in rows}
            findings = session.execute(
                select(Finding.entity_context, Finding.data_source, Finding.title)
                .join(case_findings, case_findings.c.finding_id == Finding.finding_id)
                .where(case_findings.c.case_id == case_id)
                .order_by(Finding.finding_id)
            ).all()
            for context, _, _ in findings:
                for t, v in context_indicators(context):
                    v = v.strip()
                    key = make_key(t, v) or f"{t}:{v}"
                    if t in _ENTITY_TYPES or not v or key in seen:
                        continue
                    seen.add(key)
                    row = {
                        "case_id": case_id,
                        "ioc_type": t,
                        "value": v,
                        "source": LINKED_SOURCE,
                    }
                    rows.append((CaseIOCSchema.dump(row), t, v))

            # matching_cases reads a hostname only against a finding with both.
            has_title = any(src and (title or "").strip() for _, src, title in findings)
            indexed = self._indexed_keys(session, case_id)
            marked = marked_keys(session, [case_id]).get(case_id, set())

            out = []
            for row, t, v in rows:
                if ioc_type and (t or "").lower() != ioc_type.lower():
                    continue
                key = make_key(t, v)
                pair = split_key(key) if key else None
                use = intake_use(pair[0]) if pair else None
                if use == "same_title" and not has_title:
                    use = None
                fk = fold_key(key)
                is_marked = fk in marked
                if is_marked:
                    row["is_false_positive"] = True
                row["intake"] = use or "none"
                row["linked"] = (key in indexed) if use and not is_marked else None
                out.append(row)
            return out

    @staticmethod
    def _indexed_keys(session: Session, case_id: str) -> set:
        """The keys intake reads for a Case: its rows in the shared index."""
        rows = session.execute(
            select(SharedIOC.ioc_type, SharedIOC.value).where(
                SharedIOC.case_id == case_id
            )
        )
        return {f"{t}:{v}" for t, v in rows}

    def export_iocs_json(self, case_id: str, session: Optional[Session] = None) -> str:
        """
        Export case IOCs as JSON.

        Args:
            case_id: Case ID
            session: Database session (optional)

        Returns:
            JSON string
        """
        with unit_of_work(session) as session:
            iocs = self.get_case_iocs(case_id, session=session)

            ioc_list = []
            for ioc in iocs:
                ioc_list.append(
                    {
                        "type": ioc.ioc_type,
                        "value": ioc.value,
                        "threat_level": ioc.threat_level,
                        "confidence": ioc.confidence,
                        "source": ioc.source,
                        "first_seen": (
                            ioc.first_seen.isoformat() if ioc.first_seen else None
                        ),
                        "last_seen": (
                            ioc.last_seen.isoformat() if ioc.last_seen else None
                        ),
                        "tags": ioc.tags,
                        "context": ioc.context,
                    }
                )

            return json.dumps(ioc_list, indent=2)

    def export_iocs_csv(self, case_id: str, session: Optional[Session] = None) -> str:
        """
        Export case IOCs as CSV.

        Args:
            case_id: Case ID
            session: Database session (optional)

        Returns:
            CSV string
        """
        with unit_of_work(session) as session:
            iocs = self.get_case_iocs(case_id, session=session)

            lines = ["type,value,threat_level,confidence,source,first_seen,last_seen"]
            for ioc in iocs:
                lines.append(
                    f'{ioc.ioc_type},{ioc.value},{ioc.threat_level or ""},'
                    f'{ioc.confidence or ""},{ioc.source or ""},'
                    f'{ioc.first_seen.isoformat() if ioc.first_seen else ""},'
                    f'{ioc.last_seen.isoformat() if ioc.last_seen else ""}'
                )

            return "\n".join(lines)

    def export_iocs_stix(self, case_id: str, session: Optional[Session] = None) -> Dict:
        """
        Export case IOCs in STIX 2.1 format.

        Args:
            case_id: Case ID
            session: Database session (optional)

        Returns:
            STIX bundle dictionary
        """
        with unit_of_work(session) as session:
            iocs = self.get_case_iocs(case_id, session=session)

            # Build STIX bundle
            bundle = {"type": "bundle", "id": f"bundle--{case_id}", "objects": []}

            for ioc in iocs:
                # Map IOC type to STIX type
                stix_type = self._map_ioc_to_stix_type(ioc.ioc_type)

                # STIX string literals escape backslash and single quote.
                literal = str(ioc.value).replace("\\", "\\\\").replace("'", "\\'")
                stix_obj = {
                    "type": "indicator",
                    "id": f"indicator--{ioc.ioc_id}",
                    "created": ioc.created_at.isoformat() + "Z",
                    "modified": ioc.updated_at.isoformat() + "Z",
                    "pattern": f"[{stix_type}:value = '{literal}']",
                    "pattern_type": "stix",
                    "valid_from": (
                        (ioc.first_seen.isoformat() + "Z")
                        if ioc.first_seen
                        else utcnow().isoformat() + "Z"
                    ),
                }

                if ioc.threat_level:
                    stix_obj["labels"] = [ioc.threat_level]

                bundle["objects"].append(stix_obj)

            return bundle

    def _map_ioc_to_stix_type(self, ioc_type: str) -> str:
        """Map IOC type to STIX observable type."""
        mapping = {
            "ip": "ipv4-addr",
            "ipv4": "ipv4-addr",
            "ipv6": "ipv6-addr",
            "domain": "domain-name",
            "url": "url",
            "email": "email-addr",
            "hash": "file",
            "md5": "file",
            "sha1": "file",
            "sha256": "file",
            "file_name": "file",
        }
        return mapping.get(ioc_type.lower(), "x-custom-observable")
