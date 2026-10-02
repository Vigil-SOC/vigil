"""
Case Evidence Service - Evidence and artifact management.

Handles file storage, chain of custody, and evidence tracking.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from core.storage.models import CaseEvidence
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)


class CaseEvidenceService:
    """Service for managing case evidence."""

    def __init__(self, storage_path: str = "./evidence"):
        """
        Initialize the evidence service.

        Args:
            storage_path: Path to evidence storage directory
        """
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

    def calculate_file_hashes(self, file_path: Path) -> Dict[str, str]:
        """
        Calculate MD5 and SHA256 hashes of a file.

        Args:
            file_path: Path to file

        Returns:
            Dictionary with md5 and sha256 hashes
        """
        md5_hash = hashlib.md5()
        sha256_hash = hashlib.sha256()

        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                md5_hash.update(chunk)
                sha256_hash.update(chunk)

        return {"md5": md5_hash.hexdigest(), "sha256": sha256_hash.hexdigest()}

    def resolve_stored_file(self, file_path: str) -> Path:
        """
        Resolve a caller-supplied path against the evidence store.

        Raises:
            ValueError: if the path escapes the store (absolute, ``..``, or a symlink out)
        """
        root = os.path.realpath(self.storage_path)
        full_path = os.path.realpath(os.path.join(root, file_path))
        if not full_path.startswith(root + os.sep):
            raise ValueError(
                f"file_path must be inside the evidence store: {file_path!r}"
            )
        return Path(full_path)

    def add_evidence(
        self,
        case_id: str,
        evidence_type: str,
        name: str,
        collected_by: str,
        description: Optional[str] = None,
        file_path: Optional[str] = None,
        source: Optional[str] = None,
        tags: Optional[List[str]] = None,
        session: Optional[Session] = None,
    ) -> Optional[CaseEvidence]:
        """
        Add evidence to a case.

        Args:
            case_id: Case ID
            evidence_type: Type of evidence
            name: Evidence name
            collected_by: Who collected the evidence
            description: Evidence description
            file_path: Path to stored file (if applicable)
            source: Evidence source
            tags: Tags
            session: Database session (optional)

        Returns:
            Created CaseEvidence or None

        Raises:
            ValueError: if file_path points outside the evidence store
        """
        full_path = self.resolve_stored_file(file_path) if file_path else None
        try:
            with unit_of_work(session) as session:
                # Calculate file hashes if file exists
                file_hash_md5 = None
                file_hash_sha256 = None
                file_size = None

                if full_path is not None:
                    if full_path.is_file():
                        hashes = self.calculate_file_hashes(full_path)
                        file_hash_md5 = hashes["md5"]
                        file_hash_sha256 = hashes["sha256"]
                        file_size = full_path.stat().st_size

                # Initialize chain of custody
                chain_of_custody = [
                    {
                        "timestamp": utcnow().isoformat(),
                        "action": "collected",
                        "user": collected_by,
                        "notes": "Evidence collected and added to case",
                    }
                ]

                evidence = CaseEvidence(
                    case_id=case_id,
                    evidence_type=evidence_type,
                    name=name,
                    description=description,
                    file_path=file_path,
                    file_size=file_size,
                    file_hash_md5=file_hash_md5,
                    file_hash_sha256=file_hash_sha256,
                    source=source,
                    collected_by=collected_by,
                    collected_at=utcnow(),
                    chain_of_custody=chain_of_custody,
                    tags=tags or [],
                )

                session.add(evidence)

                logger.info(f"Added evidence {evidence.evidence_id} to case {case_id}")
                return evidence

        except Exception as e:
            logger.error(f"Error adding evidence: {e}")
            return None

    def add_chain_of_custody_entry(
        self,
        evidence_id: int,
        action: str,
        user: str,
        notes: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> bool:
        """
        Add an entry to evidence chain of custody.

        Args:
            evidence_id: Evidence ID
            action: Action taken (accessed, transferred, analyzed, etc.)
            user: User who performed the action
            notes: Additional notes
            session: Database session (optional)

        Returns:
            True if successful
        """
        try:
            with unit_of_work(session) as session:
                evidence = (
                    session.query(CaseEvidence)
                    .filter(CaseEvidence.evidence_id == evidence_id)
                    .first()
                )

                if not evidence:
                    logger.error(f"Evidence {evidence_id} not found")
                    return False

                # Add new entry to chain of custody
                entry = {
                    "timestamp": utcnow().isoformat(),
                    "action": action,
                    "user": user,
                    "notes": notes or "",
                }

                chain = evidence.chain_of_custody or []
                chain.append(entry)
                evidence.chain_of_custody = chain

                # Mark as updated to trigger ORM update
                session.merge(evidence)

                logger.info(f"Added chain of custody entry for evidence {evidence_id}")
                return True

        except Exception as e:
            logger.error(f"Error adding chain of custody entry: {e}")
            return False

    def get_case_evidence(
        self,
        case_id: str,
        evidence_type: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> List[CaseEvidence]:
        """
        Get all evidence for a case.

        Args:
            case_id: Case ID
            evidence_type: Filter by evidence type
            session: Database session (optional)

        Returns:
            List of CaseEvidence objects
        """
        with unit_of_work(session) as session:
            query = session.query(CaseEvidence).filter(CaseEvidence.case_id == case_id)

            if evidence_type:
                query = query.filter(CaseEvidence.evidence_type == evidence_type)

            return query.order_by(CaseEvidence.collected_at.desc()).all()
