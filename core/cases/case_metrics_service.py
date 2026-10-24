"""
Case Metrics Service - Calculate and track case performance metrics.

Handles MTTD, MTTR, MTTA, SLA compliance, and analyst performance metrics.
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from core.cases.case_status import CLOSED
from core.storage.models import Case, CaseMetrics, CaseSLA, CaseTask
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)


class CaseMetricsService:
    """Service for calculating and managing case metrics."""

    def __init__(self):
        """Initialize the metrics service."""

    def calculate_case_metrics(
        self, case_id: str, session: Optional[Session] = None
    ) -> Optional[CaseMetrics]:
        """
        Calculate or update metrics for a case.

        Args:
            case_id: Case ID
            session: Database session (optional)

        Returns:
            CaseMetrics object or None
        """
        try:
            with unit_of_work(session) as session:
                case = session.query(Case).filter(Case.case_id == case_id).first()
                if not case:
                    return None

                # Get or create metrics record
                metrics = (
                    session.query(CaseMetrics)
                    .filter(CaseMetrics.case_id == case_id)
                    .first()
                )

                if not metrics:
                    metrics = CaseMetrics(case_id=case_id)
                    session.add(metrics)

                # Calculate time-based metrics
                case_created = case.created_at

                # Time to respond (first activity/comment)
                if case.activities and len(case.activities) > 0:
                    first_activity = min(
                        activity.get("timestamp", case_created.isoformat())
                        for activity in case.activities
                    )
                    first_activity_dt = datetime.fromisoformat(
                        first_activity.replace("Z", "+00:00")
                    )
                    metrics.time_to_respond = int(
                        (first_activity_dt - case_created).total_seconds()
                    )

                if case.status == CLOSED:
                    if case.updated_at:
                        metrics.time_to_resolve = int(
                            (case.updated_at - case_created).total_seconds()
                        )

                # Count activities
                from core.storage.models import CaseComment, CaseEvidence, CaseIOC

                metrics.comment_count = (
                    session.query(CaseComment)
                    .filter(CaseComment.case_id == case_id)
                    .count()
                )

                metrics.evidence_count = (
                    session.query(CaseEvidence)
                    .filter(CaseEvidence.case_id == case_id)
                    .count()
                )

                metrics.ioc_count = (
                    session.query(CaseIOC).filter(CaseIOC.case_id == case_id).count()
                )

                metrics.task_count = (
                    session.query(CaseTask).filter(CaseTask.case_id == case_id).count()
                )

                # Calculate total work hours from tasks
                tasks = (
                    session.query(CaseTask).filter(CaseTask.case_id == case_id).all()
                )
                total_hours = sum(task.actual_hours or 0 for task in tasks)
                metrics.total_work_hours = total_hours if total_hours > 0 else None

                # Get SLA compliance
                case_sla = (
                    session.query(CaseSLA).filter(CaseSLA.case_id == case_id).first()
                )

                if case_sla:
                    metrics.response_sla_met = case_sla.response_sla_met
                    metrics.resolution_sla_met = case_sla.resolution_sla_met

                    # Overall SLA met if both are met (or not applicable)
                    response_ok = (
                        case_sla.response_sla_met
                        or case_sla.response_completed_at is None
                    )
                    resolution_ok = (
                        case_sla.resolution_sla_met
                        or case_sla.resolution_completed_at is None
                    )
                    metrics.sla_met = response_ok and resolution_ok

                logger.info(f"Updated metrics for case {case_id}")
                return metrics

        except Exception as e:
            logger.error(f"Error calculating case metrics: {e}")
            return None
