"""
Case IOC Service - Indicator of Compromise management.

Handles IOC tracking, enrichment, deduplication, and export.
"""

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import Session

from core.cases import case_trail
from core.storage.models import CaseIOC
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)


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
            if case_trail.propagates(exc, joined):
                raise
            logger.error("Error adding IOC: %s", exc)
            return None

    def add_iocs(
        self,
        case_id: str,
        iocs: List[Dict],
        session: Session,
        added_by: Optional[str] = None,
    ) -> List[CaseIOC]:
        """Add a batch in the caller's transaction: one ``case.ioc.bulk``
        entry with the count of new IOCs, not one per IOC. Raises on failure."""
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
            if case_trail.propagates(exc, joined):
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
            is_false_positive=False,
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
            query = session.query(CaseIOC).filter(CaseIOC.case_id == case_id)

            if ioc_type:
                query = query.filter(CaseIOC.ioc_type == ioc_type)

            if active_only:
                query = query.filter(CaseIOC.is_active.is_(True))

            return query.order_by(CaseIOC.threat_level.desc()).all()

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
