"""
Case Search Service - Advanced search and filtering.

Handles full-text search, complex queries, and saved searches.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session

from core.storage.case_repository import CaseRepository
from core.storage.models import (
    Case,
    CaseComment,
    CaseEvidence,
    CaseIOC,
    Finding,
    case_findings,
)
from core.storage.schemas import CaseSchema
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)


class CaseSearchService:
    """Service for searching cases."""

    def __init__(self):
        """Initialize the search service."""

    def search_cases(
        self,
        query_text: Optional[str] = None,
        status: Optional[List[str]] = None,
        priority: Optional[List[str]] = None,
        assignee: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        mitre_techniques: Optional[List[str]] = None,
        created_after: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
        updated_after: Optional[datetime] = None,
        updated_before: Optional[datetime] = None,
        has_sla_breach: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
        session: Optional[Session] = None,
    ) -> Dict:
        """
        Search cases with advanced filters.

        Args:
            query_text: Full-text search query
            status: List of statuses to filter by
            priority: List of priorities to filter by
            assignee: List of assignees to filter by
            tags: List of tags (case must have all)
            mitre_techniques: List of MITRE techniques
            created_after: Created after date
            created_before: Created before date
            updated_after: Updated after date
            updated_before: Updated before date
            has_sla_breach: Filter by SLA breach status
            limit: Maximum results to return
            offset: Results offset for pagination
            session: Database session (optional)

        Returns:
            Dictionary with results and metadata
        """
        with unit_of_work(session) as session:
            cases, total_count = CaseRepository(session).search(
                query_text=query_text,
                status=status,
                priority=priority,
                assignee=assignee,
                tags=tags,
                mitre_techniques=mitre_techniques,
                created_after=created_after,
                created_before=created_before,
                updated_after=updated_after,
                updated_before=updated_before,
                has_sla_breach=has_sla_breach,
                limit=limit,
                offset=offset,
            )

            return {
                "results": CaseSchema.dump_many(cases),
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + len(cases)) < total_count,
            }

    def search_full_text(
        self,
        query_text: str,
        search_comments: bool = True,
        search_evidence: bool = True,
        limit: int = 50,
        session: Optional[Session] = None,
    ) -> Dict:
        """
        Perform full-text search across cases and related entities.

        Args:
            query_text: Search query
            search_comments: Include comments in search
            search_evidence: Include evidence in search
            limit: Maximum results
            session: Database session (optional)

        Returns:
            Dictionary with search results
        """
        with unit_of_work(session) as session:
            search_pattern = f"%{query_text}%"
            results = {"cases": [], "comments": [], "evidence": []}

            # Search cases
            cases = (
                session.query(Case)
                .filter(
                    or_(
                        Case.title.ilike(search_pattern),
                        Case.description.ilike(search_pattern),
                    )
                )
                .limit(limit)
                .all()
            )

            results["cases"] = [
                {
                    "case_id": case.case_id,
                    "title": case.title,
                    "description": case.description,
                    "match_type": "case",
                }
                for case in cases
            ]

            # Search comments
            if search_comments:
                comments = (
                    session.query(CaseComment)
                    .filter(CaseComment.content.ilike(search_pattern))
                    .limit(limit)
                    .all()
                )

                results["comments"] = [
                    {
                        "case_id": comment.case_id,
                        "comment_id": comment.comment_id,
                        "author": comment.author,
                        "content": comment.content,
                        "match_type": "comment",
                    }
                    for comment in comments
                ]

            # Search evidence
            if search_evidence:
                evidence_items = (
                    session.query(CaseEvidence)
                    .filter(
                        or_(
                            CaseEvidence.name.ilike(search_pattern),
                            CaseEvidence.description.ilike(search_pattern),
                        )
                    )
                    .limit(limit)
                    .all()
                )

                results["evidence"] = [
                    {
                        "case_id": evidence.case_id,
                        "evidence_id": evidence.evidence_id,
                        "name": evidence.name,
                        "description": evidence.description,
                        "match_type": "evidence",
                    }
                    for evidence in evidence_items
                ]

            return results

    def search_by_ioc(
        self,
        ioc_value: str,
        ioc_type: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> List[Dict]:
        """
        Find cases containing a specific IOC, or linked to an alert naming it.

        Args:
            ioc_value: IOC value to search for
            ioc_type: Optional IOC type filter
            session: Database session (optional)

        Returns:
            List of cases containing the IOC, each once
        """
        pattern = f"%{ioc_value}%"
        with unit_of_work(session) as session:
            query = session.query(CaseIOC.case_id).filter(CaseIOC.value.ilike(pattern))

            if ioc_type:
                query = query.filter(CaseIOC.ioc_type == ioc_type)

            ioc_ids = {row.case_id for row in query.all()}
            case_ids = set(ioc_ids)

            # Hosts, users and IPs mostly live on the linked findings, not in
            # CaseIOC rows; match them as the findings list search does.
            if not ioc_type:
                linked = session.execute(
                    select(case_findings.c.case_id)
                    .join(Finding, Finding.finding_id == case_findings.c.finding_id)
                    .where(cast(Finding.entity_context, String).ilike(pattern))
                    .distinct()
                )
                case_ids.update(linked.scalars())

            cases = session.query(Case).filter(Case.case_id.in_(case_ids)).all()

            results = CaseSchema.dump_many(cases)
            for row in results:
                if row["case_id"] not in ioc_ids:
                    row["match_type"] = "alert"
            return results
