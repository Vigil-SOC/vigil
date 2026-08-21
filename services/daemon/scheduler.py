"""Task scheduler for periodic daemon operations."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from core.config import get_settings
from services.daemon.config import SchedulerConfig

logger = logging.getLogger(__name__)


def _sandbox_poll_enabled() -> bool:
    return get_settings().sandbox_auto_submit


def _sandbox_poll_interval() -> int:
    return max(30, get_settings().sandbox_poll_interval)


@dataclass
class ScheduledTask:
    """Represents a scheduled task."""

    name: str
    func: Callable
    interval: int  # seconds
    last_run: Optional[datetime] = None
    enabled: bool = True
    run_on_start: bool = False


class TaskScheduler:
    """Manages periodic tasks for the SOC daemon."""

    def __init__(self, config: SchedulerConfig):
        self.config = config
        self._tasks: List[ScheduledTask] = []

        # Services (lazy loaded)
        self._data_service = None
        self._claude_service = None
        # The orchestrator's intake, handed over by the daemon: a scheduled hunt is
        # one more item on the queue that already owns the run and its budget.
        self._investigation_queue: Optional[asyncio.Queue] = None

        # Stats
        self.stats = {
            "tasks_run": 0,
            "threat_hunts": 0,
            "reports_generated": 0,
            "cleanups_run": 0,
            "errors": 0,
        }

        # Register default tasks
        self._register_default_tasks()

    def set_investigation_queue(self, queue: asyncio.Queue):
        """Give the scheduler the orchestrator's intake, as the processor has."""
        self._investigation_queue = queue

    def _register_default_tasks(self):
        """Register default scheduled tasks."""
        if self.config.threat_hunt_enabled:
            self._tasks.append(
                ScheduledTask(
                    name="threat_hunt",
                    func=self._run_threat_hunt,
                    interval=self.config.threat_hunt_interval,
                    enabled=True,
                    run_on_start=False,
                )
            )

        if self.config.report_generation_enabled:
            self._tasks.append(
                ScheduledTask(
                    name="weekly_report",
                    func=self._generate_report,
                    interval=self.config.report_interval,
                    enabled=True,
                    run_on_start=False,
                )
            )

        if self.config.cleanup_enabled:
            self._tasks.append(
                ScheduledTask(
                    name="cleanup",
                    func=self._run_cleanup,
                    interval=self.config.cleanup_interval,
                    enabled=True,
                    run_on_start=False,
                )
            )

        # Health check task (every 5 minutes)
        self._tasks.append(
            ScheduledTask(
                name="health_check",
                func=self._run_health_check,
                interval=300,
                enabled=True,
                run_on_start=True,
            )
        )

        # Sandbox poller — only runs when auto-submit is enabled
        if _sandbox_poll_enabled():
            self._tasks.append(
                ScheduledTask(
                    name="sandbox_poll",
                    func=self._run_sandbox_poll,
                    interval=_sandbox_poll_interval(),
                    enabled=True,
                    run_on_start=False,
                )
            )

        # Threat-feed poller — only runs when the Cloudforce One integration is enabled.
        try:
            from services.daemon.threat_feed_poller import ThreatFeedPoller

            if ThreatFeedPoller.is_enabled():
                self._tasks.append(
                    ScheduledTask(
                        name="threat_feed_poll",
                        func=self._run_threat_feed_poll,
                        interval=ThreatFeedPoller.poll_interval_seconds(),
                        enabled=True,
                        run_on_start=True,
                    )
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Threat feed poller unavailable: {e}")

    def _init_services(self):
        """Initialize required services."""
        try:
            from core.storage.database_data_service import DatabaseDataService

            self._data_service = DatabaseDataService()
            logger.info("Database service initialized for scheduler")
        except Exception as e:
            logger.error(f"Failed to initialize database service: {e}")

        try:
            from core.llm.harness.claude import ClaudeService

            self._claude_service = ClaudeService()
            logger.info("Claude service initialized for scheduler")
        except Exception as e:
            logger.warning(f"Failed to initialize Claude service: {e}")

    async def run(self, shutdown_event: asyncio.Event):
        """Run the scheduler loop."""
        logger.info("Task scheduler starting...")
        self._init_services()

        # Run startup tasks
        for task in self._tasks:
            if task.run_on_start and task.enabled:
                try:
                    await task.func()
                    task.last_run = datetime.utcnow()
                except Exception as e:
                    logger.error(f"Startup task {task.name} failed: {e}")

        # Main scheduling loop
        while not shutdown_event.is_set():
            now = datetime.utcnow()

            for task in self._tasks:
                if not task.enabled:
                    continue

                # Check if task should run
                should_run = (
                    task.last_run is None
                    or (now - task.last_run).total_seconds() >= task.interval
                )

                if should_run:
                    try:
                        logger.info(f"Running scheduled task: {task.name}")
                        await task.func()
                        task.last_run = now
                        self.stats["tasks_run"] += 1
                    except Exception as e:
                        logger.error(f"Scheduled task {task.name} failed: {e}")
                        self.stats["errors"] += 1

            # Check every minute
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=60)
                break
            except asyncio.TimeoutError:
                pass

        logger.info("Task scheduler stopped")

    async def _run_threat_hunt(self):
        """Open a hypothesis-driven hunt on the orchestrator's intake.

        Queued rather than run here: the orchestrator owns the investigation
        record, the budget and the reconcile, and a second path to any of those
        would be a second set of guardrails.
        """
        logger.info("Starting scheduled threat hunt...")
        self.stats["threat_hunts"] += 1

        if self._investigation_queue is None:
            logger.warning(
                "No investigation queue; the scheduled hunt cannot be opened"
            )
            return

        hypothesis = self._hunt_hypothesis()
        await self._investigation_queue.put(
            {
                "type": "manual",
                "workflow_id": "threat-hunt",
                "trigger_type": "scheduled",
                "priority": "low",
                "finding_ids": [],
                "hypothesis": hypothesis,
            }
        )
        logger.info(
            "Queued a scheduled threat hunt: %s",
            hypothesis or "the definition's hypotheses",
        )

    # What this hunt is out to test, read off the techniques the estate is showing, so
    # a nightly hunt follows it rather than repeating one fixed question.
    def _hunt_hypothesis(self) -> str:
        if not self._data_service:
            return ""
        try:
            findings = self._data_service.get_findings(limit=500)
        except Exception:  # noqa: BLE001 -- a hunt with no steer is still a hunt
            logger.exception("could not read findings to steer the scheduled hunt")
            return ""

        named = [
            str(entry["technique"])
            for entry in (self._get_top_techniques(findings, 3) if findings else [])
            if entry.get("technique")
        ]
        if not named:
            return ""
        return (
            f"Activity consistent with {', '.join(named)} is present in the estate "
            "and has not been explained"
        )

    async def _generate_report(self):
        """Generate periodic summary report."""
        logger.info("Generating scheduled report...")
        self.stats["reports_generated"] += 1

        if not self._data_service:
            logger.warning("Data service not available for report generation")
            return

        # Gather data for report
        findings = self._data_service.get_findings()
        cases = self._data_service.get_cases()

        # Calculate time range (last week)
        now = datetime.utcnow()
        week_ago = now - timedelta(days=7)

        # Filter to recent findings
        recent_findings = [
            f for f in findings if self._parse_timestamp(f.get("timestamp")) >= week_ago
        ]

        # Build report
        report = {
            "generated_at": now.isoformat(),
            "period_start": week_ago.isoformat(),
            "period_end": now.isoformat(),
            "summary": {
                "total_findings": len(recent_findings),
                "total_cases": len(cases),
                "critical_findings": len(
                    [f for f in recent_findings if f.get("severity") == "critical"]
                ),
                "high_findings": len(
                    [f for f in recent_findings if f.get("severity") == "high"]
                ),
            },
            "top_techniques": self._get_top_techniques(recent_findings, 5),
            "data_sources": self._get_data_source_breakdown(recent_findings),
        }

        logger.info(f"Report generated: {report['summary']}")

        # Could send report via email/Slack here
        return report

    def _parse_timestamp(self, ts: Any) -> datetime:
        """Parse timestamp to datetime."""
        if isinstance(ts, datetime):
            return ts
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(
                    ts.replace("Z", "+00:00").replace("+00:00", "")
                )
            except ValueError:
                pass
        return datetime.min

    def _get_top_techniques(self, findings: List[Dict], limit: int) -> List[Dict]:
        """Get top MITRE techniques from findings."""
        technique_counts = {}
        for finding in findings:
            mitre = finding.get("mitre_predictions", {})
            for technique in mitre.keys():
                technique_counts[technique] = technique_counts.get(technique, 0) + 1

        sorted_techniques = sorted(technique_counts.items(), key=lambda x: -x[1])[
            :limit
        ]
        return [{"technique": t, "count": c} for t, c in sorted_techniques]

    def _get_data_source_breakdown(self, findings: List[Dict]) -> Dict[str, int]:
        """Get finding counts by data source."""
        source_counts = {}
        for finding in findings:
            source = finding.get("data_source", "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1
        return source_counts

    async def _run_cleanup(self):
        """Clean up old data."""
        logger.info("Running scheduled cleanup...")
        self.stats["cleanups_run"] += 1

        # Calculate cutoff date
        cutoff = datetime.utcnow() - timedelta(days=self.config.cleanup_retention_days)

        # For now, just log what would be cleaned
        # In production, would delete old findings/processed events
        logger.info(f"Cleanup would remove data older than {cutoff.isoformat()}")

        # Dedup sets are pruned by RedisDedupSet itself (TTL + size cap)

        return {"cutoff_date": cutoff.isoformat()}

    async def _run_sandbox_poll(self):
        """Advance pending sandbox submissions to completed reports."""
        try:
            from services.daemon.sandbox_poller import SandboxPoller
        except Exception as e:
            logger.warning(f"Sandbox poller unavailable: {e}")
            return

        poller = SandboxPoller(data_service=self._data_service)
        stats = await poller.run_once()
        if stats.get("completed") or stats.get("expired") or stats.get("errors"):
            logger.info(f"Sandbox poll: {stats}")
        return stats

    async def _run_threat_feed_poll(self):
        """Pull Cloudforce One STIX/TAXII indicators into threat_indicators."""
        try:
            from services.daemon.threat_feed_poller import ThreatFeedPoller
        except Exception as e:
            logger.warning(f"Threat feed poller unavailable: {e}")
            return
        if not ThreatFeedPoller.is_enabled():
            return
        poller = ThreatFeedPoller()
        return await poller.run_once()

    async def _run_health_check(self):
        """Run system health check."""
        logger.info("Running health check...")

        health = {
            "timestamp": datetime.utcnow().isoformat(),
            "status": "healthy",
            "components": {},
        }

        # Check database
        try:
            if self._data_service:
                findings = self._data_service.get_findings()
                health["components"]["database"] = {
                    "status": "healthy",
                    "findings_count": len(findings) if findings else 0,
                }
            else:
                health["components"]["database"] = {"status": "unavailable"}
        except Exception as e:
            health["components"]["database"] = {"status": "error", "error": str(e)}
            health["status"] = "degraded"

        # Check Claude service
        try:
            if self._claude_service:
                health["components"]["claude"] = {"status": "healthy"}
            else:
                health["components"]["claude"] = {"status": "unavailable"}
        except Exception as e:
            health["components"]["claude"] = {"status": "error", "error": str(e)}

        logger.info(f"Health check: {health['status']}")
        return health
