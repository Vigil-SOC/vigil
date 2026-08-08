"""
Case Metrics Service - Calculate and track case performance metrics.

Handles MTTD, MTTR, MTTA, SLA compliance, and analyst performance metrics.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_

from core.storage.models import Case, CaseMetrics, CaseSLA, CaseTask
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)


class CaseMetricsService:
    """Service for calculating and managing case metrics."""
    
    def __init__(self):
        """Initialize the metrics service."""
    
    def calculate_case_metrics(
        self,
        case_id: str,
        session: Optional[Session] = None
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
                metrics = session.query(CaseMetrics).filter(
                    CaseMetrics.case_id == case_id
                ).first()

                if not metrics:
                    metrics = CaseMetrics(case_id=case_id)
                    session.add(metrics)

                # Calculate time-based metrics
                case_created = case.created_at

                # Time to respond (first activity/comment)
                if case.activities and len(case.activities) > 0:
                    first_activity = min(
                        activity.get('timestamp', case_created.isoformat())
                        for activity in case.activities
                    )
                    first_activity_dt = datetime.fromisoformat(
                        first_activity.replace('Z', '+00:00')
                    )
                    metrics.time_to_respond = int(
                        (first_activity_dt - case_created).total_seconds()
                    )

                # Time to resolve (case closed/resolved)
                if case.status in ['resolved', 'closed']:
                    if case.updated_at:
                        metrics.time_to_resolve = int(
                            (case.updated_at - case_created).total_seconds()
                        )

                # Count activities
                from core.storage.models import CaseComment, CaseEvidence, CaseIOC

                metrics.comment_count = session.query(CaseComment).filter(
                    CaseComment.case_id == case_id
                ).count()

                metrics.evidence_count = session.query(CaseEvidence).filter(
                    CaseEvidence.case_id == case_id
                ).count()

                metrics.ioc_count = session.query(CaseIOC).filter(
                    CaseIOC.case_id == case_id
                ).count()

                metrics.task_count = session.query(CaseTask).filter(
                    CaseTask.case_id == case_id
                ).count()

                # Calculate total work hours from tasks
                tasks = session.query(CaseTask).filter(
                    CaseTask.case_id == case_id
                ).all()
                total_hours = sum(
                    task.actual_hours or 0 for task in tasks
                )
                metrics.total_work_hours = total_hours if total_hours > 0 else None

                # Get SLA compliance
                case_sla = session.query(CaseSLA).filter(
                    CaseSLA.case_id == case_id
                ).first()

                if case_sla:
                    metrics.response_sla_met = case_sla.response_sla_met
                    metrics.resolution_sla_met = case_sla.resolution_sla_met

                    # Overall SLA met if both are met (or not applicable)
                    response_ok = (
                        case_sla.response_sla_met == True or
                        case_sla.response_completed_at is None
                    )
                    resolution_ok = (
                        case_sla.resolution_sla_met == True or
                        case_sla.resolution_completed_at is None
                    )
                    metrics.sla_met = response_ok and resolution_ok

                logger.info(f"Updated metrics for case {case_id}")
                return metrics

        except Exception as e:
            logger.error(f"Error calculating case metrics: {e}")
            return None
    
    def get_dashboard_metrics(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        session: Optional[Session] = None
    ) -> Dict:
        """
        Get dashboard metrics for cases.
        
        Args:
            start_date: Start date filter
            end_date: End date filter
            session: Database session (optional)
        
        Returns:
            Dictionary with dashboard metrics
        """
        with unit_of_work(session) as session:
            # Build base query
            query = session.query(Case)

            if start_date:
                query = query.filter(Case.created_at >= start_date)
            if end_date:
                query = query.filter(Case.created_at <= end_date)

            all_cases = query.all()

            # Count by status
            status_counts = {}
            for case in all_cases:
                status = case.status
                status_counts[status] = status_counts.get(status, 0) + 1

            # Count by priority
            priority_counts = {}
            for case in all_cases:
                priority = case.priority
                priority_counts[priority] = priority_counts.get(priority, 0) + 1

            # Calculate average resolution time
            resolved_cases = [c for c in all_cases if c.status in ['resolved', 'closed']]
            avg_resolution_time = None
            if resolved_cases:
                total_time = 0
                count = 0
                for case in resolved_cases:
                    metrics = session.query(CaseMetrics).filter(
                        CaseMetrics.case_id == case.case_id
                    ).first()
                    if metrics and metrics.time_to_resolve:
                        total_time += metrics.time_to_resolve
                        count += 1

                if count > 0:
                    avg_resolution_time = total_time / count

            # SLA compliance
            total_with_sla = session.query(CaseSLA).count()
            sla_met_count = session.query(CaseSLA).filter(
                CaseSLA.resolution_sla_met == True
            ).count()

            sla_compliance_rate = (
                (sla_met_count / total_with_sla * 100)
                if total_with_sla > 0 else 0.0
            )

            # Cases opened per day (last 30 days)
            thirty_days_ago = datetime.utcnow() - timedelta(days=30)
            recent_cases = session.query(Case).filter(
                Case.created_at >= thirty_days_ago
            ).all()

            cases_per_day = {}
            for case in recent_cases:
                day = case.created_at.date().isoformat()
                cases_per_day[day] = cases_per_day.get(day, 0) + 1

            return {
                'total_cases': len(all_cases),
                'status_breakdown': status_counts,
                'priority_breakdown': priority_counts,
                'average_resolution_time_seconds': avg_resolution_time,
                'sla_compliance_rate': sla_compliance_rate,
                'cases_per_day': cases_per_day,
                'resolved_cases_count': len(resolved_cases),
                'open_cases_count': len([c for c in all_cases if c.status in ['open', 'in-progress', 'investigating']])
            }
    
    def get_analyst_performance(
        self,
        analyst_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        session: Optional[Session] = None
    ) -> Dict:
        """
        Get performance metrics for an analyst.
        
        Args:
            analyst_id: Analyst user ID
            start_date: Start date filter
            end_date: End date filter
            session: Database session (optional)
        
        Returns:
            Dictionary with analyst performance metrics
        """
        with unit_of_work(session) as session:
            # Get analyst's cases
            query = session.query(Case).filter(Case.assignee == analyst_id)

            if start_date:
                query = query.filter(Case.created_at >= start_date)
            if end_date:
                query = query.filter(Case.created_at <= end_date)

            cases = query.all()

            # Calculate metrics
            total_cases = len(cases)
            resolved_cases = len([c for c in cases if c.status in ['resolved', 'closed']])
            open_cases = len([c for c in cases if c.status in ['open', 'in-progress', 'investigating']])

            # Average resolution time
            avg_resolution_time = None
            if resolved_cases > 0:
                total_time = 0
                count = 0
                for case in cases:
                    if case.status in ['resolved', 'closed']:
                        metrics = session.query(CaseMetrics).filter(
                            CaseMetrics.case_id == case.case_id
                        ).first()
                        if metrics and metrics.time_to_resolve:
                            total_time += metrics.time_to_resolve
                            count += 1

                if count > 0:
                    avg_resolution_time = total_time / count

            # SLA compliance
            analyst_slas = []
            for case in cases:
                case_sla = session.query(CaseSLA).filter(
                    CaseSLA.case_id == case.case_id
                ).first()
                if case_sla:
                    analyst_slas.append(case_sla)

            sla_met_count = len([s for s in analyst_slas if s.resolution_sla_met == True])
            sla_compliance = (
                (sla_met_count / len(analyst_slas) * 100)
                if len(analyst_slas) > 0 else 0.0
            )

            return {
                'analyst_id': analyst_id,
                'total_cases': total_cases,
                'resolved_cases': resolved_cases,
                'open_cases': open_cases,
                'resolution_rate': (resolved_cases / total_cases * 100) if total_cases > 0 else 0.0,
                'average_resolution_time_seconds': avg_resolution_time,
                'sla_compliance_rate': sla_compliance
            }
    
    
    def get_case_velocity(
        self,
        days: int = 30,
        session: Optional[Session] = None
    ) -> Dict:
        """
        Get case velocity (cases opened vs closed over time).
        
        Args:
            days: Number of days to analyze
            session: Database session (optional)
        
        Returns:
            Dictionary with velocity data
        """
        with unit_of_work(session) as session:
            start_date = datetime.utcnow() - timedelta(days=days)

            # Cases opened
            opened_cases = session.query(Case).filter(
                Case.created_at >= start_date
            ).all()

            # Cases closed
            closed_cases = session.query(Case).filter(
                and_(
                    Case.updated_at >= start_date,
                    Case.status.in_(['resolved', 'closed'])
                )
            ).all()

            # Group by day
            opened_by_day = {}
            for case in opened_cases:
                day = case.created_at.date().isoformat()
                opened_by_day[day] = opened_by_day.get(day, 0) + 1

            closed_by_day = {}
            for case in closed_cases:
                day = case.updated_at.date().isoformat()
                closed_by_day[day] = closed_by_day.get(day, 0) + 1

            # Calculate net velocity
            all_days = set(list(opened_by_day.keys()) + list(closed_by_day.keys()))
            velocity = {}
            for day in all_days:
                opened = opened_by_day.get(day, 0)
                closed = closed_by_day.get(day, 0)
                velocity[day] = {
                    'opened': opened,
                    'closed': closed,
                    'net': opened - closed
                }

            return {
                'period_days': days,
                'total_opened': len(opened_cases),
                'total_closed': len(closed_cases),
                'velocity_by_day': velocity
            }

