"""Task scheduler for periodic daemon operations."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from core.config import get_settings
from core.llm.bifrost.admin import refresh_gateway_rates, run_gateway_rates_refresher
from core.storage.connection import get_db_manager
from core.time import utcnow
from core.workflows.enablement import is_enabled
from core.workflows.routing import SCHEDULED_WORKFLOW
from services.daemon.config import SchedulerConfig
from services.daemon.probes import inject_probes, score_probes
from services.daemon.threat_feed_poller import ThreatFeedPoller

logger = logging.getLogger(__name__)

# Each sync reads every repo in full; hourly sees a deleted rule soon enough.
DETECTION_RULES_SYNC_INTERVAL = 3600
# Once a day, mitre_analyst turns the listed detection gaps into Cases (#3755).
DETECTION_GAPS_WORKFLOW = "agent-run-mitre_analyst"
DETECTION_GAPS_INTERVAL = 86400


def _sandbox_poll_enabled() -> bool:
    return get_settings().sandbox_auto_submit


def _sandbox_poll_interval() -> int:
    return max(30, get_settings().sandbox_poll_interval)


def _last_scheduled_at(workflow_id: str) -> Optional[datetime]:
    """When ``workflow_id`` was last queued by the scheduler; None if never or the read failed.

    Intel rows share kind "schedule" but must not hold back the hunt.
    """
    try:
        from sqlalchemy import func

        from core.storage.models import IntakeTrigger

        with get_db_manager().session_scope() as session:
            return (
                session.query(func.max(IntakeTrigger.created_at))
                .filter(
                    IntakeTrigger.kind == "schedule",
                    IntakeTrigger.payload["trigger_type"].astext == "scheduled",
                    IntakeTrigger.payload["workflow_id"].astext == workflow_id,
                )
                .scalar()
            )
    except Exception as e:  # noqa: BLE001 -- a hunt with no history still runs
        logger.warning("could not read when %s was last queued: %s", workflow_id, e)
        return None


@dataclass
class ScheduledTask:
    """Represents a scheduled task."""

    name: str
    func: Callable
    interval: int  # seconds
    last_run: Optional[datetime] = None


class TaskScheduler:
    """Manages periodic tasks for the SOC daemon."""

    def __init__(self, config: SchedulerConfig):
        self.config = config
        self._tasks: List[ScheduledTask] = []
        # The processor's input queue; probes go on it like polled findings.
        self._processor_queue: Optional[asyncio.Queue] = None

        # Services (lazy loaded)
        self._data_service = None

        # Stats
        self.stats = {
            "tasks_run": 0,
            "threat_hunts": 0,
            "probes_injected": 0,
            "probes_scored": 0,
            "cleanups_run": 0,
            "errors": 0,
        }

        # Register default tasks
        self._register_default_tasks()

    def _register_default_tasks(self):
        """Register default scheduled tasks."""
        # Always registered: each tick asks whether the hunt is turned off.
        self._tasks.append(
            ScheduledTask(
                name="threat_hunt",
                func=self._run_threat_hunt,
                interval=self.config.threat_hunt_interval,
            )
        )

        self._tasks.append(
            ScheduledTask(
                name="detection_gaps",
                func=self._run_detection_gaps,
                interval=DETECTION_GAPS_INTERVAL,
            )
        )

        self._tasks.append(
            ScheduledTask(
                name="cleanup",
                func=self._run_cleanup,
                interval=self.config.cleanup_interval,
            )
        )

        # Hourly tick; the day-scoped finding_id makes the injection once a day.
        if self.config.probes_enabled:
            self._tasks.append(
                ScheduledTask(
                    name="probe_sweep",
                    func=self._run_probe_sweep,
                    interval=self.config.probe_interval,
                )
            )

        # Sandbox poller — only runs when auto-submit is enabled
        if _sandbox_poll_enabled():
            self._tasks.append(
                ScheduledTask(
                    name="sandbox_poll",
                    func=self._run_sandbox_poll,
                    interval=_sandbox_poll_interval(),
                )
            )

        self._tasks.append(
            ScheduledTask(
                name="threat_feed_poll",
                func=self._run_threat_feed_poll,
                interval=ThreatFeedPoller.poll_interval_seconds(),
            )
        )

        self._tasks.append(
            ScheduledTask(
                name="detection_rules_sync",
                func=self._run_detection_rules_sync,
                interval=DETECTION_RULES_SYNC_INTERVAL,
            )
        )

    def set_processor_queue(self, queue: asyncio.Queue):
        """Set the processor's input queue that probe sweeps inject onto."""
        self._processor_queue = queue

    def _init_services(self):
        """Initialize required services."""
        try:
            from core.storage.database_data_service import DatabaseDataService

            self._data_service = DatabaseDataService()
            logger.info("Database service initialized for scheduler")
        except Exception as e:
            logger.error(f"Failed to initialize database service: {e}")

    async def run(self, shutdown_event: asyncio.Event):
        """Run the scheduler loop."""
        logger.info("Task scheduler starting...")
        self._init_services()
        # Pricing reads this process's own copy of the gateway's rates.
        await refresh_gateway_rates()
        rates_refresher = asyncio.create_task(run_gateway_rates_refresher())

        # The hunt's and the gaps run's clocks survive a restart; the other tasks
        # run on the first tick.
        queued = {
            "threat_hunt": SCHEDULED_WORKFLOW,
            "detection_gaps": DETECTION_GAPS_WORKFLOW,
        }
        for task in self._tasks:
            if task.name in queued:
                task.last_run = await asyncio.to_thread(
                    _last_scheduled_at, queued[task.name]
                )

        # Main scheduling loop
        while not shutdown_event.is_set():
            now = utcnow()

            for task in self._tasks:
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

        rates_refresher.cancel()
        logger.info("Task scheduler stopped")

    async def _run_threat_hunt(self):
        """Open a hypothesis-driven hunt on the orchestrator's intake.

        Queued rather than run here: the orchestrator owns the investigation
        record, the budget and the reconcile, and a second path to any of those
        would be a second set of guardrails.
        """
        if not is_enabled(SCHEDULED_WORKFLOW):
            logger.info(
                "Scheduled threat hunt skipped: %s is turned off", SCHEDULED_WORKFLOW
            )
            return
        logger.info("Starting scheduled threat hunt...")
        self.stats["threat_hunts"] += 1

        hypothesis = self._hunt_hypothesis()
        from services.daemon.orchestrator import insert_intake_trigger

        insert_intake_trigger(
            kind="schedule",
            priority="low",
            payload={
                "workflow_id": SCHEDULED_WORKFLOW,
                "trigger_type": "scheduled",
                "finding_ids": [],
                "hypothesis": hypothesis,
            },
        )
        logger.info(
            "Queued a scheduled threat hunt: %s",
            hypothesis or "the definition's hypotheses",
        )

    async def _run_detection_gaps(self):
        """Queue a mitre_analyst run on the gaps identify_gaps lists since the stored
        marker, at or above the floor and not already carried by an open Case, biggest
        first; none when there are none, unless OpenCTI is configured: then recent
        intel is checked every day.

        A schedule intake with no case_id mints a Case, so an empty report with no
        intel to read queues nothing. The marker moves to this run's start only once
        the run has queued, or found nothing to queue.
        """
        if not is_enabled(DETECTION_GAPS_WORKFLOW):
            logger.info(
                "Daily detection gaps run skipped: %s is turned off",
                DETECTION_GAPS_WORKFLOW,
            )
            return
        from core.detections import gap_run
        from core.detections.tools import get_security_detection_tools

        started = datetime.now(timezone.utc)
        marker = gap_run.read_marker(started)
        # The ledger window reaches back to the marker, however long ago it was.
        since_days = max(gap_run.FIRST_RUN_DAYS, (started - marker.since).days + 1)
        # Off the loop: the report reads the ledger and Findings synchronously.
        report = await asyncio.to_thread(
            asyncio.run,
            get_security_detection_tools().identify_gaps(
                since_days=since_days, since=marker.since
            ),
        )
        gaps = gap_run.gaps_in(report)
        raised = gap_run.to_raise(gaps, marker, gap_run.open_case_techniques())
        after = gap_run.next_marker(started, marker, gaps, raised)
        from core.integrations.mcp.client import process_mcp_client

        # Connecting is the configured check (enabled, set up, credentials), and
        # binds OpenCTI's tools to the run queued below: the daemon dials nothing else.
        client = process_mcp_client()
        try:
            # Bounded: a wedged server must not hold up every other scheduled task.
            intel = bool(client) and await asyncio.wait_for(
                client.connect_to_server("opencti"), timeout=30
            )
        except asyncio.TimeoutError:
            logger.warning("OpenCTI did not answer; the gaps run skips threat intel")
            intel = False
        if not (raised or intel):
            logger.info(
                "Daily detection gaps run: no new gaps at or above %d, nothing queued",
                gap_run.GAP_FLOOR,
            )
            gap_run.write_marker(after)
            return

        hypothesis = (
            "Turn the detection gaps identify_gaps lists into Cases with checked "
            "rule drafts, so each real gap is tracked and has a rule in shadow."
        )
        if raised:
            hypothesis += " New gaps, biggest first: " + "; ".join(
                g.line() for g in raised
            )
        if intel:
            hypothesis += (
                " Threat intel: with opencti_opencti_search_entities (empty query, newest "
                "first) read the intrusion sets and reports OpenCTI updated most "
                "recently, pass the techniques they use or name to "
                "identify_gaps(techniques=...), and treat each id in "
                "techniques_checked.gaps as a gap; techniques_checked.unknown lists ids "
                "that are not active ATT&CK techniques: never a gap, never a Case."
            )
        from services.daemon.orchestrator import insert_intake_trigger

        insert_intake_trigger(
            kind="schedule",
            priority="low",
            payload={
                "workflow_id": DETECTION_GAPS_WORKFLOW,
                "trigger_type": "scheduled",
                "finding_ids": [],
                "hypothesis": hypothesis,
                # The minted Case carries them, so the next run sees them covered.
                "case_techniques": gap_run.case_techniques(raised),
            },
        )
        gap_run.write_marker(after)
        logger.info("Queued the daily detection gaps run: %s", hypothesis)

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

    async def _run_cleanup(self):
        """Clean up old data."""
        logger.info("Running scheduled cleanup...")
        self.stats["cleanups_run"] += 1

        # Findings and processed events are intentionally not pruned here:
        # retention for them is a Settings phase 2 decision, not this
        # sweep's (#1741). The cutoff below drives only the read log sweep.
        cutoff = utcnow() - timedelta(days=self.config.cleanup_retention_days)

        # Dedup sets are pruned by RedisDedupSet itself (TTL + size cap)

        # Approvals nobody will ever answer (#675). Off-thread because each
        # expiry is its own write and the sweep is unbounded, while this runs on
        # the daemon's event loop.
        from core.response.checkpoints import expire_stale

        expired = await asyncio.to_thread(
            expire_stale, self.config.approval_expiry_days
        )
        if expired:
            logger.info("Cleanup expired %d unanswered approvals", expired)

        # The episodic read log (#732), the one part of the memory tier with a
        # retention policy. Off-thread for the same reason the sweep above is.
        from core.memory.recall import expire_read_log

        reads = await asyncio.to_thread(expire_read_log, cutoff)
        if reads:
            logger.info("Cleanup removed %d episodic read log rows", reads)

        logger.info(
            "Cleanup complete: expired %d unanswered approvals, removed %d "
            "episodic read log rows",
            expired,
            reads,
        )

        return {
            "cutoff_date": cutoff.isoformat(),
            "approvals_expired": expired,
            "read_log_removed": reads,
        }

    async def _run_probe_sweep(self):
        """Score the probes past their hour (#924), then queue today's (#923)."""
        if self._processor_queue is None or not self._data_service:
            logger.warning(
                "Probe sweep skipped: processor queue or database unavailable"
            )
            return 0
        scored = await asyncio.to_thread(score_probes, self._data_service)
        self.stats["probes_scored"] += scored
        injected = await inject_probes(self._processor_queue, self._data_service)
        self.stats["probes_injected"] += injected
        return injected

    async def _run_sandbox_poll(self):
        """Advance pending sandbox submissions to completed reports."""
        try:
            from services.daemon.sandbox_poller import SandboxPoller
        except Exception as e:
            logger.warning(f"Sandbox poller unavailable: {e}")
            return

        poller = SandboxPoller(data_service=self._data_service)
        stats = await poller.run_once()
        if any(stats.get(k) for k in ("completed", "expired", "failed", "errors")):
            logger.info(f"Sandbox poll: {stats}")
        return stats

    async def _run_threat_feed_poll(self):
        """Pull Cloudforce One STIX/TAXII indicators into threat_indicators."""
        return await ThreatFeedPoller().run_once()

    async def _run_detection_rules_sync(self):
        """Run the overlay copies (rollout), record new, changed and removed rules
        from every configured repo, then move the copies' stages.

        The copies search first, so Sees spends only what they leave of each
        SIEM's hourly budget (decision 13)."""
        from core.detections.inventory import rollout
        from core.detections.inventory.sync import sync_repos

        queue = self._processor_queue
        report = await asyncio.to_thread(rollout.run_copies, queue is not None)
        try:
            await asyncio.to_thread(sync_repos)
        finally:
            # The runs already moved last_success: their findings go out regardless.
            await asyncio.to_thread(rollout.run_sweep, report)
            # Put on this event loop, never from the worker thread.
            for finding in report.findings:
                await queue.put(
                    {
                        "type": "finding",
                        "source": finding["data_source"],
                        "data": finding,
                        "timestamp": utcnow().isoformat(),
                    }
                )
        return report
