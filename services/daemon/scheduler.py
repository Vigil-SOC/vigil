"""Task scheduler for periodic daemon operations."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from core.config import get_settings
from core.daemon_config import SchedulerConfig
from core.llm.bifrost.admin import refresh_gateway_rates, run_gateway_rates_refresher
from core.storage.connection import get_db_manager
from core.time import utcnow
from core.workflows.enablement import is_enabled
from core.workflows.routing import DETECTION_GAPS_WORKFLOW, SCHEDULED_WORKFLOW
from services.daemon.probes import inject_probes, score_probes
from services.daemon.threat_feed_poller import ThreatFeedPoller

logger = logging.getLogger(__name__)

# Each sync reads every repo in full; hourly sees a deleted rule soon enough.
DETECTION_RULES_SYNC_INTERVAL = 3600
DETECTION_GAPS_INTERVAL = 86400
SITE_REFRESH_INTERVAL = 86400


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
        # Set by the daemon: counts one scheduler-loop pass on /health.
        self.note_pass: Optional[Callable[[], None]] = None

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

        # Not in run()'s restored clocks: it runs at startup, and the row's
        # "seen" state keeps a restart from queueing it twice.
        self._tasks.append(
            ScheduledTask(
                name="site_refresh",
                func=self._run_site_refresh,
                interval=SITE_REFRESH_INTERVAL,
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

        # Hourly tick; the trail check inside makes the entry once a day, across
        # replicas and restarts (#4177).
        self._tasks.append(
            ScheduledTask(
                name="mfa_coverage",
                func=self._run_mfa_coverage,
                interval=3600,
            )
        )

        # Same shape: the once-per-stale-period check is inside (#4175).
        self._tasks.append(
            ScheduledTask(
                name="breached_list",
                func=self._run_breached_list,
                interval=3600,
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

        # Asked often; the tick itself runs a check only when the last one, here or
        # on another replica, is an interval old. A skip still counts as a run
        # here, so asking at the full interval would let a check slip by up to one.
        if self.config.supervisory_enabled:
            from core.supervisory.worker import poll_seconds

            self._tasks.append(
                ScheduledTask(
                    name="supervisory_check",
                    func=self._run_supervisory_check,
                    interval=poll_seconds(self.config.supervisory_interval),
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

        # Main scheduling loop. A task that blocks holds the counter.
        while not shutdown_event.is_set():
            if self.note_pass is not None:
                self.note_pass()
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
        from core.storage.intake_queue import insert_intake_trigger

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
        """The daily detection gaps run (``services.daemon.gaps.start_gaps_run``)."""
        from services.daemon.gaps import NOT_QUEUED, QUEUED, start_gaps_run

        outcome = await start_gaps_run()
        if outcome.blind_spots:
            # Not handed to the run, so this line is where a new one shows.
            logger.info(
                "Daily detection gaps run: new blind spots, no rule drafted: %s",
                "; ".join(outcome.blind_spots),
            )
        if outcome.status == NOT_QUEUED:
            logger.error("Daily detection gaps run: %s", outcome.message)
        elif outcome.status == QUEUED:
            logger.info("Queued the daily detection gaps run: %s", outcome.message)
        else:
            logger.info("Daily detection gaps run: %s", outcome.message)

    async def _run_site_refresh(self):
        """The daily site refresh (``core.site_proposal.start_refresh``)."""
        from core.site_proposal import NOT_QUEUED, QUEUED, start_refresh

        outcome = await start_refresh()
        if outcome["status"] == NOT_QUEUED:
            logger.error("Daily site refresh: %s", outcome["message"])
        elif outcome["status"] == QUEUED:
            logger.info(
                "Queued the daily site refresh (intake row %s)", outcome["intake_id"]
            )
        else:
            logger.info("Daily site refresh: %s", outcome["message"])

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

        # Entity facts past their time to live, or whose Case closed (#4741).
        from core.integrations import entity_facts

        facts = await asyncio.to_thread(entity_facts.sweep)

        logger.info(
            "Cleanup complete: expired %d unanswered approvals, removed %d "
            "episodic read log rows, removed %d entity facts",
            expired,
            reads,
            sum(facts.values()),
        )

        return {
            "cutoff_date": cutoff.isoformat(),
            "approvals_expired": expired,
            "read_log_removed": reads,
            "entity_facts_removed": facts,
        }

    async def _run_supervisory_check(self):
        """The supervisory model's check: hold and release, then run each check."""
        from core.supervisory import worker

        done = await asyncio.to_thread(worker.tick)
        if done is not None:
            logger.info("Supervisory check: %s", done)

    async def _run_mfa_coverage(self):
        """Today's count of people who can sign in without MFA, on the trail."""
        from core.auth.mfa_coverage import record_daily

        if await asyncio.to_thread(record_daily):
            logger.info("Recorded today's count of people without MFA")

    async def _run_breached_list(self):
        """Record, once per stale period, that the breached-password list is over 90 days old."""
        from core.auth.breached_list import record_daily

        if await asyncio.to_thread(record_daily):
            logger.info("Recorded that the breached-password list is stale")

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
        """Pull every declared TAXII feed's indicators into threat_indicators."""
        return await ThreatFeedPoller().run_once()

    async def _run_detection_rules_sync(self):
        """Run the overlay copies (rollout), record new, changed and removed rules
        from every configured repo, judge the repo versions the import check
        holds, then move the copies' stages.

        The copies search first, so Sees spends only what they leave of each
        SIEM's hourly budget (decision 13)."""
        from core.detections.inventory import import_check, rollout
        from core.detections.inventory.sync import sync_repos

        queue = self._processor_queue
        report = await asyncio.to_thread(rollout.run_copies, queue is not None)
        try:
            await asyncio.to_thread(sync_repos)
            # Field lists only: no replay, and Splunk's one search from its budget.
            await asyncio.to_thread(import_check.sweep)
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
