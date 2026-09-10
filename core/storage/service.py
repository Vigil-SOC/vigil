"""
Database service layer for Vigil SOC.

Provides high-level database operations for cases, findings, and related entities.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from core.exceptions import default_on_error
from core.storage.case_repository import CaseRepository
from core.storage.connection import get_db_manager
from core.storage.models import (
    AIDecisionLog,
    Case,
    Finding,
    FindingMitrePrediction,
)
from core.storage.schemas import FindingSchema
from core.time import utcnow

logger = logging.getLogger(__name__)

_UNSET = object()


def _numeric_prediction_items(mitre_predictions: Any) -> List[tuple[str, float]]:
    """Persist numeric map values only; keys stay text (tactic names included)."""
    if not isinstance(mitre_predictions, dict):
        return []
    items: List[tuple[str, float]] = []
    for key, value in mitre_predictions.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        items.append((str(key), float(value)))
    return items


def _set_mitre_prediction_rows(finding: Finding, mitre_predictions: Any) -> None:
    finding.mitre_prediction_rows.clear()
    for technique_id, confidence in _numeric_prediction_items(mitre_predictions):
        finding.mitre_prediction_rows.append(
            FindingMitrePrediction(
                technique_id=technique_id,
                confidence=confidence,
            )
        )


def findings_by_technique_stmt(technique_id: str, limit: Optional[int] = None):
    """Findings predicting ``technique_id``, highest confidence first."""
    stmt = (
        select(Finding)
        .join(
            FindingMitrePrediction,
            FindingMitrePrediction.finding_id == Finding.finding_id,
        )
        .where(FindingMitrePrediction.technique_id == technique_id)
        .order_by(FindingMitrePrediction.confidence.desc())
        .options(selectinload(Finding.mitre_prediction_rows))
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return stmt


class DatabaseService:
    """Service layer for database operations."""

    def __init__(self):
        """Initialize the database service."""
        self.db_manager = get_db_manager()

    # ========== Finding Operations ==========

    @default_on_error(None)
    def create_finding(
        self,
        finding_id: str,
        mitre_predictions: dict,
        anomaly_score: Optional[float],
        timestamp: Optional[datetime],
        data_source: str,
        **kwargs,
    ) -> Optional[Finding]:
        """
        Create a new finding.

        Args:
            finding_id: Unique finding ID
            mitre_predictions: MITRE ATT&CK predictions
            anomaly_score: Anomaly score (0-1), or None when the source omitted it
            timestamp: Finding timestamp, or None when the source omitted it
            data_source: Data source type
            **kwargs: Additional fields (entity_context, evidence_links, cluster_id, severity, status)

        Returns:
            Created Finding object or None if failed
        """
        with self.db_manager.session_scope() as session:
            finding = Finding(
                finding_id=finding_id,
                anomaly_score=anomaly_score,
                timestamp=timestamp,
                data_source=data_source,
                external_id=kwargs.get("external_id"),
                description=kwargs.get("description"),
                entity_context=kwargs.get("entity_context"),
                evidence_links=kwargs.get("evidence_links"),
                cluster_id=kwargs.get("cluster_id"),
                severity=kwargs.get("severity"),
                status=kwargs.get("status", "new"),
            )
            _set_mitre_prediction_rows(finding, mitre_predictions)
            session.add(finding)
            session.flush()
            session.refresh(finding)
            _ = finding.mitre_prediction_rows
            logger.info(f"Created finding: {finding_id}")
            return finding

    def bulk_create_findings(self, rows: List[Dict[str, Any]]) -> Dict[str, int]:
        """Dedup + insert many findings in one transaction; per-row create_finding
        doesn't scale to hundred-thousand-row parquet files."""
        if not rows:
            return {"imported": 0, "skipped": 0}

        by_id = {r["finding_id"]: r for r in rows}
        ids = list(by_id.keys())
        try:
            with self.db_manager.session_scope() as session:
                existing = {
                    row_id
                    for (row_id,) in session.execute(
                        select(Finding.finding_id).where(Finding.finding_id.in_(ids))
                    )
                }
                new_ids = [i for i in ids if i not in existing]
                for finding_id in new_ids:
                    r = by_id[finding_id]
                    finding = Finding(
                        finding_id=finding_id,
                        anomaly_score=r.get("anomaly_score"),
                        timestamp=r.get("timestamp"),
                        data_source=r.get("data_source", "imported"),
                        external_id=r.get("external_id"),
                        description=r.get("description"),
                        entity_context=r.get("entity_context"),
                        evidence_links=r.get("evidence_links"),
                        cluster_id=r.get("cluster_id"),
                        severity=r.get("severity"),
                        status=r.get("status", "new"),
                    )
                    _set_mitre_prediction_rows(
                        finding, r.get("mitre_predictions") or {}
                    )
                    session.add(finding)
                session.flush()
                return {"imported": len(new_ids), "skipped": len(rows) - len(new_ids)}
        except Exception as e:
            logger.error(f"Error bulk-creating findings: {e}")
            return {"imported": 0, "skipped": 0, "errors": len(rows)}

    @default_on_error(None)
    def get_finding(self, finding_id: str) -> Optional[Finding]:
        """
        Get a finding by ID.

        Args:
            finding_id: Finding ID

        Returns:
            Finding object or None if not found
        """
        with self.db_manager.session_scope() as session:
            finding = session.get(
                Finding,
                finding_id,
                options=(selectinload(Finding.mitre_prediction_rows),),
            )
            if finding:
                # Detach from session to avoid lazy loading issues
                session.expunge(finding)
            return finding

    @default_on_error(list)
    def get_findings(
        self,
        severity: Optional[str] = None,
        data_source: Optional[str] = None,
        cluster_id: Optional[str] = None,
        min_anomaly_score: Optional[float] = None,
        status: Optional[str] = None,
        search_query: Optional[str] = None,
        limit: int = 1000,
        offset: int = 0,
        sort_by: str = "timestamp",
        sort_order: str = "desc",
        timestamp_start: Optional[datetime] = None,
        timestamp_end: Optional[datetime] = None,
    ) -> List[Finding]:
        """
        Get findings with optional filters, search, and pagination.

        Args:
            severity: Filter by severity
            data_source: Filter by data source
            cluster_id: Filter by cluster ID
            min_anomaly_score: Minimum anomaly score
            status: Filter by status
            search_query: Text search across finding_id, description, entity_context
            limit: Maximum number of results
            offset: Offset for pagination
            sort_by: Column to sort by (timestamp, anomaly_score, severity)
            sort_order: Sort direction (asc, desc)

        Returns:
            List of Finding objects
        """
        with self.db_manager.session_scope() as session:
            query = select(Finding).options(selectinload(Finding.mitre_prediction_rows))

            filters = []
            if severity:
                filters.append(Finding.severity == severity)
            if data_source:
                filters.append(Finding.data_source == data_source)
            if cluster_id is not None:
                filters.append(Finding.cluster_id == cluster_id)
            if min_anomaly_score is not None:
                filters.append(Finding.anomaly_score >= min_anomaly_score)
            if status:
                filters.append(Finding.status == status)
            if timestamp_start is not None:
                filters.append(Finding.timestamp >= timestamp_start)
            if timestamp_end is not None:
                filters.append(Finding.timestamp <= timestamp_end)
            if search_query:
                from sqlalchemy import String, cast

                search_clauses = [
                    Finding.finding_id.ilike(f"%{search_query}%"),
                    cast(Finding.entity_context, String).ilike(f"%{search_query}%"),
                ]
                if hasattr(Finding, "description"):
                    search_clauses.append(
                        Finding.description.ilike(f"%{search_query}%")
                    )
                filters.append(or_(*search_clauses))

            if filters:
                query = query.where(and_(*filters))

            sort_column_map = {
                "timestamp": Finding.timestamp,
                "anomaly_score": Finding.anomaly_score,
                "severity": Finding.severity,
                "data_source": Finding.data_source,
                "status": Finding.status,
            }
            sort_col = sort_column_map.get(sort_by, Finding.timestamp)
            if sort_order == "asc":
                query = query.order_by(sort_col.asc())
            else:
                query = query.order_by(sort_col.desc())

            query = query.limit(limit).offset(offset)

            findings = session.execute(query).scalars().all()

            for finding in findings:
                session.expunge(finding)

            return findings

    @default_on_error(list)
    def get_findings_missing_enrichment(
        self, limit: int = 100, max_age_hours: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Findings stored but never enriched (ai_enrichment IS NULL), oldest first.
        Returns dicts (FindingSchema.dump inside the session) so callers get detached-safe data.
        ``max_age_hours`` bounds the working set so ancient, un-enrichable findings
        aren't retried forever."""
        with self.db_manager.session_scope() as session:
            query = (
                select(Finding)
                .options(selectinload(Finding.mitre_prediction_rows))
                .where(Finding.ai_enrichment.is_(None))
            )
            if max_age_hours:
                cutoff = utcnow() - timedelta(hours=max_age_hours)
                query = query.where(Finding.timestamp >= cutoff)
            query = query.order_by(Finding.timestamp.asc()).limit(limit)
            return FindingSchema.dump_many(session.execute(query).scalars().all())

    @default_on_error(0)
    def count_findings(
        self,
        severity: Optional[str] = None,
        data_source: Optional[str] = None,
        cluster_id: Optional[str] = None,
        min_anomaly_score: Optional[float] = None,
        status: Optional[str] = None,
        search_query: Optional[str] = None,
    ) -> int:
        """
        Count findings matching the given filters without loading rows.
        """
        with self.db_manager.session_scope() as session:
            query = select(func.count()).select_from(Finding)

            filters = []
            if severity:
                filters.append(Finding.severity == severity)
            if data_source:
                filters.append(Finding.data_source == data_source)
            if cluster_id is not None:
                filters.append(Finding.cluster_id == cluster_id)
            if min_anomaly_score is not None:
                filters.append(Finding.anomaly_score >= min_anomaly_score)
            if status:
                filters.append(Finding.status == status)
            if search_query:
                from sqlalchemy import String, cast

                filters.append(
                    or_(
                        Finding.finding_id.ilike(f"%{search_query}%"),
                        (
                            Finding.description.ilike(f"%{search_query}%")
                            if hasattr(Finding, "description")
                            else Finding.finding_id.ilike(f"%{search_query}%")
                        ),
                        cast(Finding.entity_context, String).ilike(f"%{search_query}%"),
                    )
                )

            if filters:
                query = query.where(and_(*filters))

            return session.execute(query).scalar() or 0

    @default_on_error(False)
    def update_finding(self, finding_id: str, **updates) -> bool:
        """
        Update a finding.

        Args:
            finding_id: Finding ID
            **updates: Fields to update

        Returns:
            True if successful, False otherwise
        """
        with self.db_manager.session_scope() as session:
            finding = session.get(
                Finding,
                finding_id,
                options=(selectinload(Finding.mitre_prediction_rows),),
            )
            if not finding:
                logger.warning(f"Finding not found: {finding_id}")
                return False

            mitre_predictions = updates.pop("mitre_predictions", _UNSET)

            # Unknown keys are skipped rather than rejected: the S3 sync path
            # passes whole external finding dicts. Say which, or a typo'd column
            # name is a silent no-op that still reports success.
            dropped = [k for k in updates if not hasattr(finding, k)]
            if dropped:
                logger.warning(
                    "update_finding(%s): ignoring unknown field(s) %s",
                    finding_id,
                    ", ".join(sorted(dropped)),
                )
            for key, value in updates.items():
                if hasattr(finding, key):
                    setattr(finding, key, value)

            if mitre_predictions is not _UNSET:
                _set_mitre_prediction_rows(finding, mitre_predictions)

            finding.updated_at = utcnow()
            session.flush()
            logger.info(f"Updated finding: {finding_id}")
            return True

    @default_on_error(False)
    def delete_finding(self, finding_id: str) -> bool:
        """
        Delete a finding.

        Args:
            finding_id: Finding ID

        Returns:
            True if successful, False otherwise
        """
        with self.db_manager.session_scope() as session:
            finding = session.get(Finding, finding_id)
            if not finding:
                logger.warning(f"Finding not found: {finding_id}")
                return False

            session.delete(finding)
            logger.info(f"Deleted finding: {finding_id}")
            return True

    @default_on_error(list)
    def get_findings_by_technique(
        self, technique_id: str, limit: Optional[int] = None
    ) -> List[Finding]:
        """Findings predicting ``technique_id``, ordered by confidence descending."""
        with self.db_manager.session_scope() as session:
            findings = (
                session.execute(findings_by_technique_stmt(technique_id, limit=limit))
                .scalars()
                .all()
            )
            for finding in findings:
                session.expunge(finding)
            return findings

    @default_on_error(dict)
    def get_technique_max_confidence(self) -> Dict[str, float]:
        """Max confidence per technique_id across all prediction rows."""
        with self.db_manager.session_scope() as session:
            rows = session.execute(
                select(
                    FindingMitrePrediction.technique_id,
                    func.max(FindingMitrePrediction.confidence),
                ).group_by(FindingMitrePrediction.technique_id)
            ).all()
            return {tid: float(conf) for tid, conf in rows}

    @default_on_error(list)
    def get_technique_severity_counts(
        self,
        min_confidence: float = 0.0,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[tuple]:
        """(technique_id, severity, count) from the child table."""
        with self.db_manager.session_scope() as session:
            stmt = (
                select(
                    FindingMitrePrediction.technique_id,
                    Finding.severity,
                    func.count(),
                )
                .join(
                    Finding,
                    Finding.finding_id == FindingMitrePrediction.finding_id,
                )
                .where(FindingMitrePrediction.confidence >= min_confidence)
                .group_by(FindingMitrePrediction.technique_id, Finding.severity)
            )
            if start_time is not None:
                stmt = stmt.where(Finding.timestamp >= start_time)
            if end_time is not None:
                stmt = stmt.where(Finding.timestamp <= end_time)
            return [
                (tid, severity, int(count))
                for tid, severity, count in session.execute(stmt).all()
            ]

    @default_on_error(dict)
    def get_technique_occurrence_counts(self) -> Dict[str, int]:
        """Finding-row counts per technique_id."""
        with self.db_manager.session_scope() as session:
            rows = session.execute(
                select(
                    FindingMitrePrediction.technique_id,
                    func.count(),
                ).group_by(FindingMitrePrediction.technique_id)
            ).all()
            return {tid: int(count) for tid, count in rows}

    # ========== Case Operations ==========

    @default_on_error(None)
    def create_case(
        self, case_id: str, title: str, finding_ids: List[str], **kwargs
    ) -> Optional[Case]:
        """
        Create a new case.

        Args:
            case_id: Unique case ID
            title: Case title
            finding_ids: List of finding IDs to link
            **kwargs: Additional fields (description, status, priority, assignee, tags, etc.)

        Returns:
            Created Case object or None if failed
        """
        with self.db_manager.session_scope() as session:
            # Create case
            now = utcnow()
            case = Case(
                case_id=case_id,
                title=title,
                description=kwargs.get("description", ""),
                status=kwargs.get("status", "new"),
                priority=kwargs.get("priority", "medium"),
                assignee=kwargs.get("assignee"),
                tags=kwargs.get("tags", []),
                notes=kwargs.get("notes", []),
                timeline=kwargs.get(
                    "timeline",
                    [{"timestamp": now.isoformat() + "Z", "event": "Case created"}],
                ),
                activities=kwargs.get("activities", []),
                resolution_steps=kwargs.get("resolution_steps", []),
                mitre_techniques=kwargs.get("mitre_techniques"),
            )
            session.add(case)
            session.flush()

            # Link findings
            if finding_ids:
                findings = (
                    session.execute(
                        select(Finding).where(Finding.finding_id.in_(finding_ids))
                    )
                    .scalars()
                    .all()
                )
                case.findings.extend(findings)
                session.flush()

            session.refresh(case)
            logger.info(f"Created case: {case_id} with {len(finding_ids)} findings")
            return case

    @default_on_error(None)
    def get_case(self, case_id: str, include_findings: bool = False) -> Optional[Case]:
        """
        Get a case by ID.

        Args:
            case_id: Case ID
            include_findings: If True, include full finding objects

        Returns:
            Case object or None if not found
        """
        with self.db_manager.session_scope() as session:
            case = session.get(Case, case_id)
            if case:
                # Force load findings if needed
                if include_findings:
                    for linked in case.findings:
                        _ = linked.mitre_prediction_rows
                session.expunge(case)
            return case

    @default_on_error(list)
    def get_cases(
        self,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        assignee: Optional[str] = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> List[Case]:
        """
        Get cases with optional filters.

        Args:
            status: Filter by status
            priority: Filter by priority
            assignee: Filter by assignee
            limit: Maximum number of results
            offset: Offset for pagination

        Returns:
            List of Case objects
        """
        with self.db_manager.session_scope() as session:
            cases = CaseRepository(session).find(
                status=status,
                priority=priority,
                assignee=assignee,
                limit=limit,
                offset=offset,
                order_by="created_at",
            )

            # Detach from session
            for case in cases:
                session.expunge(case)

            return cases

    @default_on_error(False)
    def update_case(self, case_id: str, **updates) -> bool:
        """
        Update a case.

        Args:
            case_id: Case ID
            **updates: Fields to update

        Returns:
            True if successful, False otherwise
        """
        with self.db_manager.session_scope() as session:
            case = session.get(Case, case_id)
            if not case:
                logger.warning(f"Case not found: {case_id}")
                return False

            # ``finding_ids`` maps to the ``findings`` relationship, not a
            # column, so the generic setattr loop below would drop it.
            if "finding_ids" in updates:
                CaseRepository(session).set_findings(
                    case, updates.pop("finding_ids") or []
                )

            # Update remaining mapped fields
            for key, value in updates.items():
                if hasattr(case, key):
                    setattr(case, key, value)

            case.updated_at = utcnow()
            session.flush()
            logger.info(f"Updated case: {case_id}")
            return True

    @default_on_error(False)
    def delete_case(self, case_id: str) -> bool:
        """
        Delete a case.

        Args:
            case_id: Case ID

        Returns:
            True if successful, False otherwise
        """
        with self.db_manager.session_scope() as session:
            case = session.get(Case, case_id)
            if not case:
                logger.warning(f"Case not found: {case_id}")
                return False

            session.delete(case)
            logger.info(f"Deleted case: {case_id}")
            return True

    @default_on_error(False)
    def add_finding_to_case(self, case_id: str, finding_id: str) -> bool:
        """
        Add a finding to a case.

        Args:
            case_id: Case ID
            finding_id: Finding ID

        Returns:
            True if successful, False otherwise
        """
        with self.db_manager.session_scope() as session:
            case = session.get(Case, case_id)
            finding = session.get(Finding, finding_id)

            if not case or not finding:
                logger.warning(f"Case or finding not found: {case_id}, {finding_id}")
                return False

            if finding not in case.findings:
                case.findings.append(finding)
                case.updated_at = utcnow()
                session.flush()
                logger.info(f"Added finding {finding_id} to case {case_id}")

            return True

    @default_on_error(False)
    def remove_finding_from_case(self, case_id: str, finding_id: str) -> bool:
        """
        Remove a finding from a case.

        Args:
            case_id: Case ID
            finding_id: Finding ID

        Returns:
            True if successful, False otherwise
        """
        with self.db_manager.session_scope() as session:
            case = session.get(Case, case_id)
            finding = session.get(Finding, finding_id)

            if not case or not finding:
                logger.warning(f"Case or finding not found: {case_id}, {finding_id}")
                return False

            if finding in case.findings:
                case.findings.remove(finding)
                case.updated_at = utcnow()
                session.flush()
                logger.info(f"Removed finding {finding_id} from case {case_id}")

            return True

    # ========== Statistics ==========

    # ========== AI Decision Log Operations ==========

    @default_on_error(None)
    def create_ai_decision(
        self,
        decision_id: str,
        agent_id: str,
        decision_type: str,
        confidence_score: float,
        reasoning: str,
        recommended_action: str,
        finding_id: Optional[str] = None,
        case_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        decision_metadata: Optional[dict] = None,
    ) -> Optional[AIDecisionLog]:
        """
        Log an AI decision for tracking and feedback.

        Args:
            decision_id: Unique decision identifier
            agent_id: ID of the agent making the decision
            decision_type: Type of decision (e.g., 'triage', 'escalate', 'isolate')
            confidence_score: AI's confidence in the decision (0-1)
            reasoning: AI's reasoning for the decision
            recommended_action: Recommended action text
            finding_id: Optional associated finding ID
            case_id: Optional associated case ID
            workflow_id: Optional workflow ID
            decision_metadata: Optional additional metadata

        Returns:
            Created AIDecisionLog or None if failed
        """
        with self.db_manager.session_scope() as session:
            decision = AIDecisionLog(
                decision_id=decision_id,
                agent_id=agent_id,
                decision_type=decision_type,
                confidence_score=confidence_score,
                reasoning=reasoning,
                recommended_action=recommended_action,
                finding_id=finding_id,
                case_id=case_id,
                workflow_id=workflow_id,
                decision_metadata=decision_metadata,
                timestamp=utcnow(),
            )

            session.add(decision)
            session.flush()

            logger.info(f"Created AI decision log: {decision_id} by {agent_id}")
            return decision

    @default_on_error(None)
    def submit_ai_decision_feedback(
        self,
        decision_id: str,
        human_reviewer: str,
        human_decision: str,
        feedback_comment: Optional[str] = None,
        accuracy_grade: Optional[float] = None,
        reasoning_grade: Optional[float] = None,
        action_appropriateness: Optional[float] = None,
        actual_outcome: Optional[str] = None,
        time_saved_minutes: Optional[int] = None,
    ) -> Optional[AIDecisionLog]:
        """
        Submit human feedback on an AI decision.

        Args:
            decision_id: Decision to provide feedback on
            human_reviewer: Name/ID of reviewer
            human_decision: Human's decision ('agree', 'disagree', 'partial')
            feedback_comment: Optional comment
            accuracy_grade: Grade for accuracy (0-1)
            reasoning_grade: Grade for reasoning quality (0-1)
            action_appropriateness: Grade for action appropriateness (0-1)
            actual_outcome: Actual outcome ('true_positive', 'false_positive', etc.)
            time_saved_minutes: Estimated time saved by AI

        Returns:
            Updated AIDecisionLog or None if failed
        """
        with self.db_manager.session_scope() as session:
            decision = (
                session.query(AIDecisionLog)
                .filter(AIDecisionLog.decision_id == decision_id)
                .first()
            )

            if not decision:
                logger.error(f"AI decision not found: {decision_id}")
                return None

            # Update feedback fields
            decision.human_reviewer = human_reviewer
            decision.human_decision = human_decision
            decision.feedback_comment = feedback_comment
            decision.accuracy_grade = accuracy_grade
            decision.reasoning_grade = reasoning_grade
            decision.action_appropriateness = action_appropriateness
            decision.actual_outcome = actual_outcome
            decision.time_saved_minutes = time_saved_minutes
            decision.feedback_timestamp = utcnow()

            session.flush()

            logger.info(
                f"Updated AI decision feedback: {decision_id} by {human_reviewer}"
            )
            return decision

    @default_on_error(None)
    def get_ai_decision(self, decision_id: str) -> Optional[AIDecisionLog]:
        """
        Get an AI decision by ID.

        Args:
            decision_id: Decision ID

        Returns:
            AIDecisionLog or None if not found
        """
        with self.db_manager.session_scope() as session:
            return (
                session.query(AIDecisionLog)
                .filter(AIDecisionLog.decision_id == decision_id)
                .first()
            )

    @default_on_error(list)
    def list_ai_decisions(
        self,
        agent_id: Optional[str] = None,
        finding_id: Optional[str] = None,
        case_id: Optional[str] = None,
        has_feedback: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AIDecisionLog]:
        """
        List AI decisions with optional filters.

        Args:
            agent_id: Filter by agent ID
            finding_id: Filter by finding ID
            case_id: Filter by case ID
            has_feedback: Filter by whether feedback exists
            limit: Maximum number of results
            offset: Offset for pagination

        Returns:
            List of AIDecisionLog objects
        """
        with self.db_manager.session_scope() as session:
            query = session.query(AIDecisionLog)

            if agent_id:
                query = query.filter(AIDecisionLog.agent_id == agent_id)

            if finding_id:
                query = query.filter(AIDecisionLog.finding_id == finding_id)

            if case_id:
                query = query.filter(AIDecisionLog.case_id == case_id)

            if has_feedback is not None:
                if has_feedback:
                    query = query.filter(AIDecisionLog.human_decision.isnot(None))
                else:
                    query = query.filter(AIDecisionLog.human_decision.is_(None))

            decisions = (
                query.order_by(AIDecisionLog.timestamp.desc())
                .limit(limit)
                .offset(offset)
                .all()
            )

            return decisions

    def get_ai_decision_stats(
        self, agent_id: Optional[str] = None, days: int = 30
    ) -> dict:
        """
        Get statistics on AI decisions and feedback.

        Args:
            agent_id: Optional filter by agent ID
            days: Number of days to look back

        Returns:
            Dictionary with statistics
        """
        try:
            with self.db_manager.session_scope() as session:
                since = utcnow() - timedelta(days=days)

                query = session.query(AIDecisionLog).filter(
                    AIDecisionLog.timestamp >= since
                )

                if agent_id:
                    query = query.filter(AIDecisionLog.agent_id == agent_id)

                # Total decisions
                total_decisions = query.count()

                # Decisions with feedback
                feedback_query = query.filter(AIDecisionLog.human_decision.isnot(None))
                total_with_feedback = feedback_query.count()

                # Agreement rate
                agree_count = feedback_query.filter(
                    AIDecisionLog.human_decision == "agree"
                ).count()

                # Average grades
                avg_accuracy = session.query(
                    func.avg(AIDecisionLog.accuracy_grade)
                ).filter(
                    AIDecisionLog.timestamp >= since,
                    AIDecisionLog.accuracy_grade.isnot(None),
                )

                if agent_id:
                    avg_accuracy = avg_accuracy.filter(
                        AIDecisionLog.agent_id == agent_id
                    )

                avg_accuracy = avg_accuracy.scalar() or 0

                # Outcome counts
                outcomes = {}
                for outcome, count in (
                    session.query(
                        AIDecisionLog.actual_outcome, func.count(AIDecisionLog.id)
                    )
                    .filter(
                        AIDecisionLog.timestamp >= since,
                        AIDecisionLog.actual_outcome.isnot(None),
                    )
                    .group_by(AIDecisionLog.actual_outcome)
                    .all()
                ):
                    outcomes[outcome] = count

                # Time saved
                total_time_saved = session.query(
                    func.sum(AIDecisionLog.time_saved_minutes)
                ).filter(
                    AIDecisionLog.timestamp >= since,
                    AIDecisionLog.time_saved_minutes.isnot(None),
                )

                if agent_id:
                    total_time_saved = total_time_saved.filter(
                        AIDecisionLog.agent_id == agent_id
                    )

                total_time_saved = total_time_saved.scalar() or 0

                return {
                    "total_decisions": total_decisions,
                    "total_with_feedback": total_with_feedback,
                    "feedback_rate": (
                        round(total_with_feedback / total_decisions, 3)
                        if total_decisions > 0
                        else 0
                    ),
                    "agreement_rate": (
                        round(agree_count / total_with_feedback, 3)
                        if total_with_feedback > 0
                        else 0
                    ),
                    "avg_accuracy_grade": round(avg_accuracy, 3),
                    "outcomes": outcomes,
                    "total_time_saved_minutes": int(total_time_saved),
                    "total_time_saved_hours": round(total_time_saved / 60, 1),
                    "period_days": days,
                }
        except Exception as e:
            logger.error(f"Error getting AI decision statistics: {e}")
            return {
                "total_decisions": 0,
                "total_with_feedback": 0,
                "feedback_rate": 0,
                "agreement_rate": 0,
                "avg_accuracy_grade": 0,
                "outcomes": {},
                "total_time_saved_minutes": 0,
                "total_time_saved_hours": 0,
                "period_days": days,
            }
