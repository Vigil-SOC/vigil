"""
Case Automation Service - Scheduled jobs and automated workflows.

Handles SLA monitoring, auto-assignment, escalation, and periodic tasks.
"""

import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from database.connection import get_db_session
from database.models import Case, CaseSLA, CaseNotification
from services.case_sla_service import CaseSLAService
from services.case_workflow_service import CaseWorkflowService
from services.case_notification_service import CaseNotificationService
from services.case_metrics_service import CaseMetricsService

logger = logging.getLogger(__name__)


class CaseAutomationService:
    """Service for automated case workflows and scheduled tasks."""
    
    def __init__(self):
        """Initialize the automation service."""
        self.sla_service = CaseSLAService()
        self.workflow_service = CaseWorkflowService()
        self.notification_service = CaseNotificationService()
        self.metrics_service = CaseMetricsService()
        self.running = False
    
    async def start(self):
        """Start all automation tasks."""
        self.running = True
        logger.info("Starting case automation service")
        
        # Start all scheduled tasks
        tasks = [
            self.sla_monitor_task(),
            self.metrics_update_task(),
            self.stale_case_detector_task(),
            self.digest_generator_task()
        ]
        
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def stop(self):
        """Stop automation service."""
        self.running = False
        logger.info("Stopping case automation service")
    
    async def sla_monitor_task(self):
        """Monitor SLAs and send alerts."""
        while self.running:
            try:
                logger.debug("Running SLA monitor")
                await self._check_sla_deadlines()
                await asyncio.sleep(60)  # Check every minute
            except Exception as e:
                logger.error(f"Error in SLA monitor task: {e}")
                await asyncio.sleep(60)
    
    async def _check_sla_deadlines(self):
        """Check SLA deadlines and send notifications."""
        session = get_db_session()
        try:
            # Get all active SLAs
            active_slas = session.query(CaseSLA).filter(
                CaseSLA.resolution_completed_at == None,
                CaseSLA.is_paused == False
            ).all()
            
            current_time = datetime.utcnow()
            
            for sla in active_slas:
                # Get SLA status
                status = self.sla_service.get_sla_status(sla.case_id, session)
                if not status:
                    continue
                
                # Check if we need to send notifications
                response_pct = status.get('response_percent_elapsed', 0)
                resolution_pct = status.get('resolution_percent_elapsed', 0)
                
                # Send notifications at 75%, 90%, 100% thresholds
                thresholds = [75, 90, 100]
                for threshold in thresholds:
                    if response_pct >= threshold and not sla.response_completed_at:
                        self.notification_service.notify_sla_warning(
                            sla.case_id, threshold, 'response', session
                        )
                    
                    if resolution_pct >= threshold and not sla.resolution_completed_at:
                        self.notification_service.notify_sla_warning(
                            sla.case_id, threshold, 'resolution', session
                        )
                
                # Mark as breached if over 100%
                if (resolution_pct >= 100 or response_pct >= 100) and not sla.breached:
                    sla.breached = True
                    sla.breach_time = current_time
                    sla.breach_reason = "SLA deadline exceeded"
                    session.commit()
            
        except Exception as e:
            logger.error(f"Error checking SLA deadlines: {e}")
            session.rollback()
        finally:
            session.close()
    
    async def metrics_update_task(self):
        """Update case metrics periodically."""
        while self.running:
            try:
                logger.debug("Running metrics update")
                await self._update_case_metrics()
                await asyncio.sleep(3600)  # Update every hour
            except Exception as e:
                logger.error(f"Error in metrics update task: {e}")
                await asyncio.sleep(3600)
    
    async def _update_case_metrics(self):
        """Update metrics for all open cases."""
        session = get_db_session()
        try:
            # Get all open cases
            open_cases = session.query(Case).filter(
                Case.status.in_(['open', 'in-progress', 'investigating'])
            ).all()
            
            for case in open_cases:
                self.metrics_service.calculate_case_metrics(case.case_id, session)
            
            logger.info(f"Updated metrics for {len(open_cases)} cases")
        except Exception as e:
            logger.error(f"Error updating case metrics: {e}")
        finally:
            session.close()
    
    async def stale_case_detector_task(self):
        """Detect and flag stale cases."""
        while self.running:
            try:
                logger.debug("Running stale case detector")
                await self._detect_stale_cases()
                await asyncio.sleep(86400)  # Check daily
            except Exception as e:
                logger.error(f"Error in stale case detector: {e}")
                await asyncio.sleep(86400)
    
    async def _detect_stale_cases(self):
        """Detect cases with no activity for extended periods."""
        session = get_db_session()
        try:
            # Define stale threshold (7 days)
            stale_threshold = datetime.utcnow() - timedelta(days=7)
            
            # Find cases not updated recently
            stale_cases = session.query(Case).filter(
                Case.status.in_(['open', 'in-progress', 'investigating']),
                Case.updated_at < stale_threshold
            ).all()
            
            for case in stale_cases:
                # Notify assignee
                if case.assignee:
                    self.notification_service.create_notification(
                        user_id=case.assignee,
                        notification_type='stale_case',
                        title='Stale Case Alert',
                        message=f'Case "{case.title}" has had no activity for 7+ days',
                        case_id=case.case_id,
                        priority='normal',
                        session=session
                    )
            
            logger.info(f"Detected {len(stale_cases)} stale cases")
        except Exception as e:
            logger.error(f"Error detecting stale cases: {e}")
        finally:
            session.close()
    
    async def digest_generator_task(self):
        """Generate daily digest emails."""
        while self.running:
            try:
                # Calculate time until next 9 AM
                now = datetime.utcnow()
                next_run = now.replace(hour=9, minute=0, second=0, microsecond=0)
                if now.hour >= 9:
                    next_run += timedelta(days=1)
                
                wait_seconds = (next_run - now).total_seconds()
                logger.info(f"Next digest in {wait_seconds/3600:.1f} hours")
                
                await asyncio.sleep(wait_seconds)
                
                logger.info("Generating daily digest")
                await self._generate_daily_digest()
            except Exception as e:
                logger.error(f"Error in digest generator: {e}")
                await asyncio.sleep(3600)
    
    async def _generate_daily_digest(self):
        """Generate and send daily digest."""
        session = get_db_session()
        try:
            # Get metrics for last 24 hours
            yesterday = datetime.utcnow() - timedelta(days=1)
            metrics = self.metrics_service.get_dashboard_metrics(
                start_date=yesterday,
                session=session
            )
            
            # Get breached cases
            breached = self.sla_service.get_breached_cases(session)
            
            # In a real implementation, would send digest emails here
            logger.info(f"Daily digest: {metrics.get('total_cases', 0)} total cases, "
                       f"{len(breached)} breached")
        except Exception as e:
            logger.error(f"Error generating digest: {e}")
        finally:
            session.close()
    
    def auto_assign_new_case(
        self,
        case_id: str,
        available_analysts: List[str]
    ) -> Optional[str]:
        """
        Auto-assign a newly created case.
        
        Args:
            case_id: Case ID
            available_analysts: List of available analyst IDs
        
        Returns:
            Assigned analyst ID
        """
        try:
            assigned = self.workflow_service.auto_assign_case(
                case_id,
                assignment_strategy='least_workload',
                available_analysts=available_analysts
            )
            
            if assigned:
                logger.info(f"Auto-assigned case {case_id} to {assigned}")
                
                # Send notification
                self.notification_service.notify_case_assignment(
                    case_id, assigned, "System"
                )
            
            return assigned
        except Exception as e:
            logger.error(f"Error auto-assigning case {case_id}: {e}")
            return None
    
    def auto_escalate_breached_cases(self):
        """Auto-escalate cases that have breached SLA."""
        session = get_db_session()
        try:
            breached_cases = self.sla_service.get_breached_cases(session)
            
            for case_data in breached_cases:
                case_id = case_data['case_id']
                case = session.query(Case).filter(Case.case_id == case_id).first()
                
                if case and case.assignee:
                    # In real implementation, would determine escalation target
                    escalation_target = "soc_manager"  # Placeholder
                    
                    self.workflow_service.escalate_case(
                        case_id,
                        escalated_from=case.assignee,
                        escalated_to=escalation_target,
                        reason="SLA breach - automatic escalation",
                        urgency_level="critical",
                        session=session
                    )
                    
                    logger.info(f"Auto-escalated breached case {case_id}")
        except Exception as e:
            logger.error(f"Error auto-escalating cases: {e}")
            session.rollback()
        finally:
            session.close()


# Singleton instance
automation_service = CaseAutomationService()


# ---------------------------------------------------------------------------
# VStrike attack-path clustering
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _max_severity(findings: List[Dict]) -> str:
    best = "low"
    for f in findings:
        sev = (f.get("severity") or "low").lower()
        if _SEVERITY_ORDER.get(sev, 0) > _SEVERITY_ORDER.get(best, 0):
            best = sev
    return best


def cluster_findings_by_attack_path(finding_ids: List[str]) -> List[str]:
    """Group VStrike-enriched findings into cases by (segment, attack_path[0]).

    For each cluster with at least one finding, create a case titled
    "VStrike: {segment} via {initial_asset}". Returns the list of newly
    created case_ids.

    Findings without a `vstrike` sub-dict in `entity_context` are skipped —
    this function is VStrike-specific by design.
    """
    from services.database_data_service import DatabaseDataService

    data_service = DatabaseDataService()

    clusters: Dict[tuple, List[Dict]] = {}
    for fid in finding_ids:
        finding = data_service.get_finding(fid)
        if not finding:
            continue
        ctx = finding.get("entity_context") or {}
        vstrike = ctx.get("vstrike") if isinstance(ctx, dict) else None
        if not vstrike:
            continue
        segment = vstrike.get("segment") or "unknown-segment"
        attack_path = vstrike.get("attack_path") or []
        initial_asset = (
            attack_path[0]
            if attack_path
            else (vstrike.get("asset_id") or "unknown-asset")
        )
        key = (segment, initial_asset)
        clusters.setdefault(key, []).append(finding)

    created_case_ids: List[str] = []
    for (segment, initial_asset), findings in clusters.items():
        ids = [f.get("finding_id") for f in findings if f.get("finding_id")]
        if not ids:
            continue

        severity = _max_severity(findings)
        priority = severity  # treat severity as priority for VStrike cases
        mission_systems = sorted(
            {
                (f.get("entity_context") or {})
                .get("vstrike", {})
                .get("mission_system")
                for f in findings
                if (f.get("entity_context") or {})
                .get("vstrike", {})
                .get("mission_system")
            }
        )
        mission_str = (
            f" Mission systems impacted: {', '.join(mission_systems)}."
            if mission_systems
            else ""
        )
        description = (
            f"{len(findings)} enriched finding(s) from the VStrike fusion layer "
            f"across segment '{segment}'. Attack path origin: {initial_asset}."
            f"{mission_str}"
        )
        title = f"VStrike: {segment} via {initial_asset}"

        case = data_service.create_case(
            title=title,
            finding_ids=ids,
            priority=priority,
            description=description,
            status="open",
        )
        if case and case.get("case_id"):
            created_case_ids.append(case["case_id"])
            logger.info(
                "Created VStrike auto-cluster case %s (%s findings)",
                case["case_id"],
                len(ids),
            )

    return created_case_ids

