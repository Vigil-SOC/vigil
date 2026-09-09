"""Master agent orchestrator for autonomous SOC operations.

The orchestrator runs three loops:
  1. Intake loop: picks up new findings/tasks and creates investigations
  2. Supervision loop: monitors running agents, detects stuck/runaway ones
  3. Review loop: evaluates completed investigations, approves or requests rework

It does NOT maintain a persistent Claude conversation. It calls Claude
only for judgment calls (skill selection for ambiguous cases, review evaluation).
All routine operations are pure Python logic.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.agents.builtins import ORCHESTRATION_DECISION_ID, ORCHESTRATOR_ACTOR
from core.config import get_settings
from core.time import utcnow
from services.daemon.config import OrchestratorConfig

try:
    from opentelemetry.trace import SpanKind

    from core.telemetry import get_meter, get_tracer, inject_traceparent

    _tracer = get_tracer("vigil.daemon.orchestrator")
    _orch_meter = get_meter("vigil.daemon.orchestrator")
    _inv_created = _orch_meter.create_counter(
        "soc_daemon_orchestrator_investigations_created_total",
        description="Total investigations created",
        unit="1",
    )
    _inv_completed = _orch_meter.create_counter(
        "soc_daemon_orchestrator_investigations_completed_total",
        description="Total investigations completed",
        unit="1",
    )
    _inv_failed = _orch_meter.create_counter(
        "soc_daemon_orchestrator_investigations_failed_total",
        description="Total investigations failed",
        unit="1",
    )
    _dedup_prevented = _orch_meter.create_counter(
        "soc_daemon_orchestrator_dedup_prevented_total",
        description="Total investigations deduplicated",
        unit="1",
    )
    _stuck_agents = _orch_meter.create_counter(
        "soc_daemon_orchestrator_stuck_agents_total",
        description="Stuck agents detected and killed",
        unit="1",
    )
except Exception:
    _tracer = None  # type: ignore[assignment]
    _inv_created = _inv_completed = _inv_failed = _dedup_prevented = _stuck_agents = None  # type: ignore[assignment]
from core.agents.projections import read_projection, run_id_for
from core.agents.queue import build_start_job, enqueue_run
from core.integrations.mcp.client import process_mcp_client
from core.memory.entity_keys import finding_entity_keys, normalise_keys
from core.response.approval_service import ApprovalService
from core.response.checkpoints import raise_for_checkpoint
from core.workflows.hypothesis_subjects import kept_subjects
from services.daemon.plan_generator import (
    count_steps,
    generate_case_review_context,
    generate_case_review_plan,
    generate_initial_context,
    generate_initial_state,
    generate_plan,
    select_workflow,
)
from services.daemon.shared_intel import SharedIntelligence
from services.daemon.workdir import WorkdirManager

logger = logging.getLogger(__name__)


def _inv_as_dict(inv):
    """Serialize an investigation to a dict, passing through one already a dict.

    The polling loops receive investigations already serialized; the model
    branch is a safety net. Imports the schema lazily to keep this module
    importable without a database.
    """
    if isinstance(inv, dict):
        return inv
    from core.storage.schemas import InvestigationSchema

    return InvestigationSchema.dump(inv)


class Orchestrator:
    """Master agent that manages autonomous SOC investigations."""

    def __init__(
        self,
        config: OrchestratorConfig,
        approvals: Optional[ApprovalService] = None,
        mcp_client=None,
    ):
        self.config = config
        self._enabled = config.enabled
        self._shutdown_event: Optional[asyncio.Event] = None
        self._approvals = approvals or ApprovalService()
        self._mcp_client = (
            mcp_client if mcp_client is not None else process_mcp_client()
        )

        self.workdir = WorkdirManager(config.workdir_base)
        self.shared_intel = SharedIntelligence()

        self.investigation_queue: asyncio.Queue = asyncio.Queue()

        self._data_service = None
        self._claude_service = None
        self._hourly_costs: List[Dict] = []

        self.stats = {
            "investigations_created": 0,
            "investigations_completed": 0,
            "investigations_failed": 0,
            "reviews_completed": 0,
            "stuck_agents_killed": 0,
            "dedup_prevented": 0,
            "total_cost_usd": 0.0,
        }

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True
        logger.info("Orchestrator ENABLED")

    def disable(self):
        self._enabled = False
        logger.info("Orchestrator DISABLED (graceful)")

    async def kill(self):
        """Emergency stop: cancel all running agents immediately."""
        self._enabled = False
        self._abandon_in_flight()
        logger.warning("Orchestrator KILLED - all agents stopped")

    def _init_services(self):
        if self._data_service is None:
            try:
                from core.storage.database_data_service import DatabaseDataService

                self._data_service = DatabaseDataService()
                logger.info("Orchestrator: Database service initialized")
            except Exception as e:
                logger.error(f"Orchestrator: Failed to init data service: {e}")

    async def run(self, shutdown_event: asyncio.Event):
        """Main orchestrator entry point, called by SOCDaemon."""
        self._shutdown_event = shutdown_event
        self._init_services()

        if not self._enabled:
            logger.info(
                "Orchestrator loaded (disabled) - waiting for enable via UI/API"
            )

        while not shutdown_event.is_set():
            self._sync_enabled_from_db()

            if not self._enabled:
                await self._sleep(shutdown_event, 5)
                continue

            logger.info("Orchestrator starting...")
            logger.info(f"  Max concurrent agents: {self.config.max_concurrent_agents}")
            logger.info(
                f"  Max cost/investigation: ${self.config.max_cost_per_investigation}"
            )
            logger.info(
                f"  Auto-assign severities: {self.config.auto_assign_severities}"
            )
            logger.info(f"  Dry run: {self.config.dry_run}")

            tasks = [
                asyncio.create_task(self._intake_loop(shutdown_event)),
                asyncio.create_task(self._supervision_loop(shutdown_event)),
                asyncio.create_task(self._review_loop(shutdown_event)),
                asyncio.create_task(self._distil_loop(shutdown_event)),
            ]

            while not shutdown_event.is_set() and self._enabled:
                self._sync_enabled_from_db()
                await self._sleep(shutdown_event, 5)

            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            if not shutdown_event.is_set():
                logger.info(
                    "Orchestrator disabled - loops stopped, waiting for re-enable"
                )

        self._abandon_in_flight()
        logger.info("Orchestrator shutdown complete")

    def _sync_enabled_from_db(self):
        """Read the enabled state from the single ``orchestrator.settings``
        SystemConfig row (set by the API/UI toggle or the Settings page)."""
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import SystemConfig

            with get_db_manager().session_scope() as session:
                cfg = (
                    session.query(SystemConfig)
                    .filter_by(key="orchestrator.settings")
                    .first()
                )
                if cfg and isinstance(cfg.value, dict):
                    db_enabled = bool(cfg.value.get("enabled", False))
                    if db_enabled != self._enabled:
                        self._enabled = db_enabled
                        logger.info(
                            f"Orchestrator {'ENABLED' if db_enabled else 'DISABLED'} (synced from DB)"
                        )
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Intake Loop
    # -------------------------------------------------------------------------

    async def _intake_loop(self, shutdown_event: asyncio.Event):
        """Consume the investigation queue and create new investigations."""
        while not shutdown_event.is_set():
            try:
                if not self._enabled:
                    await self._sleep(shutdown_event, 10)
                    continue

                # Process queued items
                while not self.investigation_queue.empty():
                    try:
                        item = self.investigation_queue.get_nowait()
                        await self._process_intake_item(item, shutdown_event)
                    except asyncio.QueueEmpty:
                        break

                # Also pick up queued investigations from the database
                await self._pickup_queued_investigations(shutdown_event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Intake loop error: {e}", exc_info=True)

            await self._sleep(shutdown_event, self.config.loop_interval)

    async def _process_intake_item(self, item: Dict, shutdown_event: asyncio.Event):
        """Process a single item from the investigation queue."""
        item_type = item.get("type")

        if item_type == "finding":
            finding = item.get("data", {})
            await self._create_investigation_for_finding(finding, shutdown_event)
        elif item_type == "manual":
            await self._create_manual_investigation(item, shutdown_event)
        else:
            logger.warning(f"Unknown intake item type: {item_type}")

    async def _create_investigation_for_finding(
        self, finding: Dict, shutdown_event: asyncio.Event
    ):
        """Create an investigation for a finding, with dedup checks."""
        finding_id = finding.get("finding_id", "unknown")
        severity = (finding.get("severity") or "").lower()

        if severity not in self.config.auto_assign_severities:
            return

        overlapping = self.shared_intel.check_overlap(finding)
        if overlapping:
            logger.info(
                f"Finding {finding_id} overlaps with {overlapping}, adding to existing investigation"
            )
            self.stats["dedup_prevented"] += 1
            if _dedup_prevented is not None:
                _dedup_prevented.add(1)
            self._log_ai_decision(
                decision_type="dedup_prevention",
                inv_id=overlapping,
                reasoning=f"Finding {finding_id} shares entities with existing investigation {overlapping}. Skipping to avoid duplicate work.",
                action="skip_investigation",
                confidence=0.9,
            )
            return

        workflow_id = select_workflow(finding)
        self._log_ai_decision(
            decision_type="skill_selection",
            inv_id=finding_id,
            reasoning=f"Selected workflow '{workflow_id}' for finding {finding_id} (severity={severity}, title={finding.get('title', 'N/A')[:100]})",
            action=f"assign_workflow:{workflow_id}",
            confidence=0.85,
        )
        await self._create_investigation(
            workflow_id=workflow_id,
            findings=[finding],
            trigger_type="finding",
            priority=severity or "medium",
            shutdown_event=shutdown_event,
        )

    async def _create_manual_investigation(
        self, item: Dict, shutdown_event: asyncio.Event
    ):
        """Create an investigation from a manual request."""
        workflow_id = item.get("workflow_id", "incident-response")
        finding_ids = item.get("finding_ids", [])
        case_id = item.get("case_id")
        hypothesis = item.get("hypothesis")
        hypothesis_subjects = item.get("hypothesis_subjects")

        findings = []
        if self._data_service and finding_ids:
            for fid in finding_ids:
                f = self._data_service.get_finding(fid)
                if f:
                    findings.append(f)

        await self._create_investigation(
            workflow_id=workflow_id,
            findings=findings,
            # The intake is shared, so what put the item on it is the item's to say.
            trigger_type=item.get("trigger_type") or "manual",
            priority=item.get("priority", "medium"),
            case_id=case_id,
            hypothesis=hypothesis,
            hypothesis_subjects=hypothesis_subjects,
            shutdown_event=shutdown_event,
        )

    async def _create_investigation(
        self,
        workflow_id: str,
        findings: List[Dict],
        trigger_type: str,
        priority: str,
        case_id: Optional[str] = None,
        hypothesis: Optional[str] = None,
        hypothesis_subjects: Optional[Dict[str, List[str]]] = None,
        shutdown_event: Optional[asyncio.Event] = None,
    ):
        """Core investigation creation logic."""
        inv_id = f"inv-{utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
        total_steps = count_steps(workflow_id)

        workdir = self.workdir.create(inv_id)

        plan_md = generate_plan(inv_id, workflow_id, findings, case_id, hypothesis)
        self.workdir.write_file(inv_id, "plan.md", plan_md)

        state = generate_initial_state(
            inv_id, workflow_id, case_id, findings, total_steps
        )
        self.workdir.write_state(inv_id, state)

        self.workdir.write_file(
            inv_id, "context.md", generate_initial_context(findings)
        )
        # What this run is about, so the harness's keyed read has something to ask
        # on. Written even when empty: an investigation whose findings name no
        # entity asked nothing rather than being handed keys nobody minted.
        self.workdir.write_file(
            inv_id, "recall_keys.json", json.dumps(finding_entity_keys(findings))
        )
        # Beside the context and read back the same way at enqueue: what the hunt was
        # opened to test reaches its board as a hypothesis, not as prose in the brief.
        if hypothesis:
            self.workdir.write_file(inv_id, "hypotheses.txt", hypothesis)
        # What each of those claims is about, keyed by the claim. Stated by the
        # caller or absent: nothing here reads a finding and guesses, because a
        # guessed subject and a stated one are the same bytes downstream, and a
        # Verdict recalled under an entity that was never in the claim is worse
        # than one recalled under nothing.
        if hypothesis and hypothesis_subjects:
            self.workdir.write_file(
                inv_id, "hypothesis_subjects.json", json.dumps(hypothesis_subjects)
            )

        can_start_now = (
            not self.config.dry_run
            and shutdown_event
            and self._in_flight() < self.config.max_concurrent_agents
        )

        # Start root investigation span — will be the parent for all agent spans
        _inv_span = None
        _tp = ""
        try:
            if _tracer is not None:
                _inv_span = _tracer.start_span(
                    "investigation",
                    kind=SpanKind.INTERNAL,
                    attributes={
                        "vigil.investigation.id": inv_id,
                        "vigil.investigation.workflow_id": workflow_id,
                        "vigil.investigation.trigger_type": trigger_type,
                        "vigil.investigation.priority": priority,
                        "vigil.investigation.finding_count": len(findings),
                    },
                )
                _tp_carrier: Dict = {}
                inject_traceparent(_tp_carrier)
                _tp = _tp_carrier.get("traceparent", "")
        except Exception:
            pass

        inv_record = {
            "investigation_id": inv_id,
            "case_id": case_id,
            "workflow_id": workflow_id,
            "trigger_type": trigger_type,
            "trigger_ids": [
                f.get("finding_id") for f in findings if f.get("finding_id")
            ],
            "status": "assigned" if can_start_now else "queued",
            "workdir": str(workdir),
            "current_step": 1,
            "total_steps": total_steps,
            "priority": priority,
            "max_iterations": self.config.max_iterations_per_agent,
            "max_cost_usd": self.config.max_cost_per_investigation,
            "max_runtime_seconds": self.config.max_runtime_per_investigation,
            "otel_traceparent": _tp,
            "run_id": run_id_for(inv_id),
        }

        self._save_investigation(inv_record)
        self.shared_intel.register_investigation(inv_id, findings)
        self.stats["investigations_created"] += 1
        if _inv_created is not None:
            _inv_created.add(1)

        try:
            if _inv_span is not None:
                _inv_span.end()
        except Exception:
            pass

        self.workdir.append_log(
            inv_id,
            {
                "event": "investigation_created",
                "workflow_id": workflow_id,
                "trigger_type": trigger_type,
                "finding_count": len(findings),
            },
        )

        logger.info(
            f"Created investigation {inv_id} (workflow={workflow_id}, priority={priority}, steps={total_steps})"
        )

        await self._check_cross_correlations(inv_id)

        if can_start_now:
            await self._enqueue_investigation(inv_record)
        else:
            logger.info(f"Agent pool full, {inv_id} queued for pickup")

    async def _pickup_queued_investigations(self, shutdown_event: asyncio.Event):
        """Check database for investigations waiting to be assigned to agents."""
        if self.config.dry_run:
            return

        for status in ("assigned", "queued"):
            investigations = self._get_investigations_by_status(status)
            for inv in investigations:
                inv_id = inv.get("investigation_id") or (
                    inv.investigation_id if hasattr(inv, "investigation_id") else None
                )
                if not inv_id:
                    continue
                if self._in_flight() >= self.config.max_concurrent_agents:
                    return

                self._update_investigation_status(inv_id, "assigned")
                inv_dict = _inv_as_dict(inv)
                inv_dict["status"] = "assigned"
                await self._enqueue_investigation(inv_dict)

    # -------------------------------------------------------------------------
    # Supervision Loop
    # -------------------------------------------------------------------------

    async def _supervision_loop(self, shutdown_event: asyncio.Event):
        """Monitor running agents for stuck/runaway conditions."""
        while not shutdown_event.is_set():
            try:
                if not self._enabled:
                    await self._sleep(shutdown_event, 10)
                    continue

                # Before anything reads a row: the row is a copy of the ledger,
                # and a supervisor deciding on a stale copy decides on nothing.
                for status in ("executing", "waiting_approval"):
                    for inv in self._get_investigations_by_status(status):
                        reconciling = _inv_as_dict(inv).get("investigation_id")
                        if reconciling:
                            await self._reconcile(reconciling)

                waiting = self._get_investigations_by_status("waiting_approval")
                for inv in waiting:
                    inv_dict = _inv_as_dict(inv)
                    w_inv_id = inv_dict.get("investigation_id")
                    if not w_inv_id:
                        continue
                    notified_key = f"approval_notified:{w_inv_id}"
                    if not hasattr(self, "_notified_approvals"):
                        self._notified_approvals: set = set()
                    if notified_key not in self._notified_approvals:
                        self._notified_approvals.add(notified_key)
                        self._send_notification(
                            w_inv_id,
                            "approval_required",
                            f"Approval required: {w_inv_id}",
                            f"Investigation {w_inv_id} is waiting for human approval of a restricted tool.",
                            priority="high",
                        )
                        await self._send_slack_for_notification(
                            f"Approval required: {w_inv_id}",
                            f"Investigation {w_inv_id} is paused pending human approval.",
                        )

                executing = self._get_investigations_by_status("executing")
                now = utcnow()

                for inv in executing:
                    inv_dict = _inv_as_dict(inv)
                    inv_id = inv_dict.get("investigation_id")
                    if not inv_id:
                        continue

                    last_activity = inv_dict.get("last_activity_at")
                    if last_activity:
                        if isinstance(last_activity, str):
                            last_activity = datetime.fromisoformat(last_activity)
                        idle_seconds = (now - last_activity).total_seconds()
                        if idle_seconds > self.config.stale_threshold:
                            logger.warning(
                                "supervisor.kill stale inv_id=%s idle_s=%.0f "
                                "iteration=%s current_activity=%r cost_usd=%.4f "
                                "stale_threshold=%s",
                                inv_id,
                                idle_seconds,
                                inv_dict.get("iteration_count"),
                                inv_dict.get("current_activity"),
                                inv_dict.get("cost_usd", 0.0),
                                self.config.stale_threshold,
                            )
                            self._update_investigation_status(
                                inv_id, "failed", "Stale: no activity"
                            )
                            self.stats["stuck_agents_killed"] += 1
                            if _stuck_agents is not None:
                                _stuck_agents.add(1)
                            self._send_notification(
                                inv_id,
                                "agent_stuck",
                                f"Agent stuck: {inv_id}",
                                f"Agent for investigation {inv_id} was idle for {idle_seconds:.0f}s and has been terminated.",
                                priority="high",
                            )
                            await self._send_slack_for_notification(
                                f"Agent stuck: {inv_id}",
                                f"Agent idle for {idle_seconds:.0f}s, terminated.",
                            )

                    cost = inv_dict.get("cost_usd", 0.0)
                    max_cost = inv_dict.get(
                        "max_cost_usd", self.config.max_cost_per_investigation
                    )
                    if cost >= max_cost:
                        logger.warning(
                            "supervisor.kill cost inv_id=%s cost_usd=%.4f "
                            "max_cost_usd=%.4f iteration=%s current_activity=%r",
                            inv_id,
                            cost,
                            max_cost,
                            inv_dict.get("iteration_count"),
                            inv_dict.get("current_activity"),
                        )
                        # The budget seam refuses the next call at the same
                        # ceiling; this is the record catching up, not the kill.
                        self._update_investigation_status(
                            inv_id, "failed", "Cost budget exceeded"
                        )

                self._track_hourly_cost()

                if not hasattr(self, "_supervision_tick"):
                    self._supervision_tick = 0
                self._supervision_tick += 1
                if self._supervision_tick % 5 == 0:
                    for inv in executing:
                        inv_dict = _inv_as_dict(inv)
                        x_inv_id = inv_dict.get("investigation_id")
                        if x_inv_id:
                            await self._check_cross_correlations(x_inv_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Supervision loop error: {e}", exc_info=True)

            await self._sleep(shutdown_event, self.config.loop_interval // 2)

    # -------------------------------------------------------------------------
    # The run: enqueued here, driven by the agent worker, read back as a projection
    # -------------------------------------------------------------------------

    IN_FLIGHT = ("assigned", "executing", "waiting_approval")

    def _in_flight(self) -> int:
        return sum(len(self._get_investigations_by_status(s)) for s in self.IN_FLIGHT)

    # A run belongs to the worker, so nothing here stops one. The record is marked
    # and the run finishes or hits its ceiling; reaping a stalled worker is #633.
    def _abandon_in_flight(self) -> None:
        for status in self.IN_FLIGHT:
            for inv in self._get_investigations_by_status(status):
                inv_id = _inv_as_dict(inv).get("investigation_id")
                if inv_id:
                    self._update_investigation_status(
                        inv_id,
                        "failed",
                        "Orchestrator stopped while the run was in flight",
                    )

    # Absent and unreadable answer the same, because both mean the run's inputs
    # are not there to be read, and a half-read investigation is not a better one.
    def _sidecar(self, inv_id: str, filename: str) -> Any:
        held = self.workdir.read_file(inv_id, filename)
        if not held:
            return None
        try:
            return json.loads(held)
        except ValueError:
            logger.warning("%s has an unreadable %s", inv_id, filename)
            return None

    # An empty map rather than None, because a JSON null reaches the agent layer
    # as a value where a missing key reads as unset.
    def _hypothesis_subjects(
        self, inv_id: str, stated: List[str]
    ) -> Dict[str, List[str]]:
        declared = self._sidecar(inv_id, "hypothesis_subjects.json")
        return {} if declared is None else kept_subjects(declared, stated)

    # Re-minted rather than trusted: the keys crossed a queue boundary as a file,
    # and one in the shared-IOC vocabulary returns no rows rather than an error.
    def _recall_keys(self, inv_id: str) -> List[str]:
        return normalise_keys(self._sidecar(inv_id, "recall_keys.json"))

    async def _enqueue_investigation(self, inv_record: Dict) -> None:
        inv_id = inv_record["investigation_id"]
        run_id = inv_record.get("run_id") or run_id_for(inv_id)
        # One per line, as the console's run modal sends them. Empty for a workflow
        # that walks phases and has no board to put them on.
        hypotheses = [
            line.strip()
            for line in (
                self.workdir.read_file(inv_id, "hypotheses.txt") or ""
            ).splitlines()
            if line.strip()
        ]
        request = {
            # The workflow resolves both layers, so no config path travels beside it.
            "playbook": f"workflow:{inv_record['workflow_id']}",
            "config": "",
            "arch": "",
            "prompt": self.workdir.read_file(inv_id, "context.md") or "",
            "hypotheses": hypotheses,
            "hypothesis_subjects": self._hypothesis_subjects(inv_id, hypotheses),
            # What the run opens its episodic read on. A hunt derives its own from
            # the hypotheses being put up; an investigation has none, so its keys
            # are the entities the findings it was opened on carry.
            "recall_keys": self._recall_keys(inv_id),
            # ORCHESTRATOR_MAX_COST and ORCHESTRATOR_MAX_RUNTIME keep their meaning
            # as the ceilings the budget seam refuses the next call at.
            "overrides": {
                "budgets": {
                    "max_calls": self.config.max_iterations_per_agent,
                    "max_cost_usd": self.config.max_cost_per_investigation,
                    "max_wall_ms": self.config.max_runtime_per_investigation * 1000,
                }
            },
        }

        try:
            job = build_start_job(
                run_id, "investigate", request, enqueued_by="orchestrator"
            )
            await enqueue_run(job)
        except Exception as exc:  # noqa: BLE001 — a queue that refuses is not a crash
            logger.error("could not enqueue %s: %s", inv_id, exc)
            self._update_investigation_status(
                inv_id, "failed", f"Could not enqueue: {exc}"
            )
            return

        self._update_investigation_status(inv_id, "executing")
        logger.info("enqueued investigation %s as run %s", inv_id, run_id)

    # The ledger is the record and this row is the copy an operator reads, so the
    # copy is reconciled from the projection rather than written alongside it.
    async def _reconcile(self, inv_id: str) -> None:
        projection = await read_projection(run_id_for(inv_id))
        if projection is None:
            return

        self._record_progress(inv_id, projection)
        checkpoint = projection.get("open_checkpoint")
        if checkpoint:
            self._raise_run_approval(inv_id, checkpoint)
            self._update_investigation_status(inv_id, "waiting_approval")
            return

        if projection.get("status") != "terminal":
            self._update_investigation_status(inv_id, "executing")
            return

        outcome = projection.get("outcome")
        reason = projection.get("reason") or ""
        if outcome == "completed":
            self._update_investigation_status(inv_id, "review_submitted", reason)
        else:
            self._update_investigation_status(inv_id, "failed", reason or str(outcome))

    # The heartbeat. A failure here is logged loudly on purpose (#147): swallowed
    # quietly, last_activity_at stops moving and the supervisor stale-kills a run
    # that is perfectly healthy.
    def _record_progress(self, inv_id: str, projection: Dict[str, Any]) -> None:
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import Investigation

            with get_db_manager().session_scope() as session:
                inv = (
                    session.query(Investigation)
                    .filter_by(investigation_id=inv_id)
                    .first()
                )
                if inv is None:
                    logger.warning("progress for %s: row not found", inv_id)
                    return
                inv.iteration_count = projection.get("iterations", 0)
                inv.last_activity_at = utcnow()
                # Null is "the gateway priced nothing", which is not zero spent.
                cost = projection.get("cost_usd")
                if cost is not None:
                    inv.cost_usd = float(cost)
        except Exception:
            logger.error("DB update for %s failed", inv_id, exc_info=True)

    def _raise_run_approval(self, inv_id: str, checkpoint: Dict[str, Any]) -> None:
        question = checkpoint.get("question") or "Approve this action?"
        raise_for_checkpoint(
            run_id=run_id_for(inv_id),
            checkpoint_id=str(checkpoint.get("checkpoint_id")),
            title=f"Approval required: {inv_id}",
            description=question,
            reason="The investigation parked on a call that needs a human",
            parameters={"investigation_id": inv_id},
        )

    def _track_hourly_cost(self):
        """Track rolling hourly cost for budget enforcement."""
        now = utcnow()
        cutoff = now - timedelta(hours=1)
        self._hourly_costs = [c for c in self._hourly_costs if c["ts"] > cutoff]
        hourly_total = sum(c["cost"] for c in self._hourly_costs)

        if hourly_total >= self.config.max_total_hourly_cost:
            logger.warning(
                f"Hourly cost ${hourly_total:.2f} exceeds limit ${self.config.max_total_hourly_cost:.2f}, pausing intake"
            )
            self._enabled = False

    # -------------------------------------------------------------------------
    # Review Loop
    # -------------------------------------------------------------------------

    async def _review_loop(self, shutdown_event: asyncio.Event):
        """Review completed investigations."""
        while not shutdown_event.is_set():
            try:
                if not self._enabled:
                    await self._sleep(shutdown_event, 10)
                    continue

                submitted = self._get_investigations_by_status("review_submitted")
                for inv in submitted:
                    inv_dict = _inv_as_dict(inv)
                    inv_id = inv_dict.get("investigation_id")
                    if not inv_id:
                        continue

                    await self._review_investigation(inv_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Review loop error: {e}", exc_info=True)

            await self._sleep(shutdown_event, self.config.loop_interval)

    # Its own loop rather than a step of the supervision one: the Distils are not
    # supervising anything, they read terminals and closures the rest of the
    # system has already left behind. Slower than the others because nothing
    # waits on either — memory is read at the start of the next run, so a write
    # that lands minutes later is a write that lands in time.
    async def _distil_loop(self, shutdown_event: asyncio.Event):
        """Turn finished hunts and closed cases into episodic memory (#731, #733)."""
        from core.memory.distil import distil_once

        while not shutdown_event.is_set():
            try:
                if not self._enabled:
                    await self._sleep(shutdown_event, 10)
                    continue

                # Both on this tick rather than in a loop of its own: neither is
                # supervising anything, both poll for work already finished, and
                # a second loop would be a second interval to keep in step.
                await self._case_distil_tick()

                written = await distil_once()
                if written["investigations"]:
                    logger.info(
                        "Distilled %s investigation(s): %s sightings, %s verdicts, %s gaps",
                        written["investigations"],
                        written["sightings"],
                        written["verdicts"],
                        written["gaps"],
                    )
                # Surfaced here and not only where they happened: one refusal in
                # a log is a line nobody reads, and a tick that wrote nothing
                # because everything failed must not look like a tick with
                # nothing to do.
                if written["refused"] or written["unreadable"] or written["failed"]:
                    logger.warning(
                        "Distil left %s investigation(s) undistilled: "
                        "%s refused, %s unreadable, %s failed",
                        written["refused"] + written["unreadable"] + written["failed"],
                        written["refused"],
                        written["unreadable"],
                        written["failed"],
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                # Logged and retried rather than swallowed into a quiet stop: a
                # Distil that dies silently is a memory that reads as empty
                # rather than as broken, and empty is what an entity nobody has
                # looked at also reads as.
                logger.error(f"Distil loop error: {e}", exc_info=True)

            await self._sleep(shutdown_event, self.config.loop_interval)

    async def _case_distil_tick(self):
        """Turn closed Cases into Verdicts (#733).

        Its own method so that a failure here costs the Case Distil and not the
        hunt one: they write different rows from different sources, and a
        closure this job cannot map must not stop a finished hunt reaching
        memory.
        """
        from core.memory.case_distil import case_distil_once

        try:
            written = await case_distil_once()
        except Exception as e:
            logger.error(f"Case Distil error: {e}", exc_info=True)
            return

        if written["cases"]:
            logger.info(
                "Distilled %s closed case(s): %s verdicts",
                written["cases"],
                written["verdicts"],
            )
        if written["withdrawn"]:
            logger.info(
                "Withdrew %s Verdict(s) from reopened case(s)", written["withdrawn"]
            )
        if written["refused"] or written["failed"]:
            logger.warning(
                "Case Distil left %s case(s) undistilled: %s refused, %s failed",
                written["refused"] + written["failed"],
                written["refused"],
                written["failed"],
            )

    async def _review_investigation(self, inv_id: str):
        """Review a completed investigation's results."""
        state = self.workdir.read_state(inv_id)

        completed_steps = state.get("completed_steps", [])
        total_steps = state.get("total_steps", 0)
        summary = state.get("summary", "")
        proposed_actions = state.get("proposed_actions", [])

        completeness = len(completed_steps) / total_steps if total_steps > 0 else 0

        if completeness >= 0.8 and summary:
            self._update_investigation_status(inv_id, "completed")
            self.stats["investigations_completed"] += 1
            if _inv_completed is not None:
                _inv_completed.add(1)
            self.stats["reviews_completed"] += 1
            self.shared_intel.close_investigation(inv_id, state.get("case_id"))

            self.workdir.append_log(
                inv_id,
                {
                    "event": "review_passed",
                    "completeness": completeness,
                    "proposed_actions_count": len(proposed_actions),
                },
            )

            logger.info(
                f"Investigation {inv_id} APPROVED ({completeness:.0%} complete, {len(proposed_actions)} actions)"
            )

            self._log_ai_decision(
                decision_type="review_approve",
                inv_id=inv_id,
                reasoning=f"Investigation completed {completeness:.0%} of steps with valid summary. {len(proposed_actions)} proposed actions.",
                action="approve",
                confidence=completeness,
            )

            self._send_notification(
                inv_id,
                "investigation_complete",
                f"Investigation {inv_id} completed",
                f"Investigation completed at {completeness:.0%} with {len(proposed_actions)} proposed actions.",
                priority="normal",
            )

            if proposed_actions:
                for action in proposed_actions:
                    if action.get("requires_approval"):
                        await self._create_approval_action(inv_id, action)

            case_id = state.get("case_id")
            if case_id and state.get("workflow_id") != "case-review":
                await self._maybe_trigger_case_review(case_id)

        else:
            notes = f"Review incomplete: {completeness:.0%} steps done."
            if not summary:
                notes += " Missing summary."
            missing = [i for i in range(1, total_steps + 1) if i not in completed_steps]
            if missing:
                notes += f" Missing steps: {missing}"

            self._update_investigation_status(inv_id, "needs_rework", notes)
            self.stats["reviews_completed"] += 1

            self.workdir.append_log(
                inv_id,
                {
                    "event": "review_needs_rework",
                    "notes": notes,
                    "completeness": completeness,
                },
            )

            logger.info(f"Investigation {inv_id} NEEDS REWORK: {notes}")

            self._log_ai_decision(
                decision_type="review_rework",
                inv_id=inv_id,
                reasoning=notes,
                action="needs_rework",
                confidence=completeness,
            )

            self._send_notification(
                inv_id,
                "investigation_needs_review",
                f"Investigation {inv_id} needs rework",
                notes,
                priority="high",
            )

    async def _create_approval_action(self, inv_id: str, action: Dict):
        """Create an approval action for proposed response."""
        try:
            from core.response.approval_service import ActionType

            service = self._approvals

            action_str = action.get("action", "unknown")
            try:
                action_type = ActionType(action_str)
            except ValueError:
                action_type = ActionType.CUSTOM

            service.create_action(
                action_type=action_type,
                title=f"Auto-investigation action: {action_str}",
                description=f"Investigation {inv_id} proposes: {action.get('reason', '')}",
                target=action.get("target", "unknown"),
                confidence=0.8,
                reason=f"[Auto-investigation {inv_id}] {action.get('reason', '')}",
                evidence=[inv_id],
                created_by=ORCHESTRATOR_ACTOR,
            )
            logger.info(f"Created approval action for {inv_id}: {action_str}")
        except Exception as e:
            logger.error(f"Failed to create approval action: {e}")

    async def _maybe_trigger_case_review(self, case_id: str):
        """Trigger a case-review agent if one hasn't already run for this case."""
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import Investigation as InvModel

            with get_db_manager().session_scope() as session:
                existing = (
                    session.query(InvModel)
                    .filter(
                        InvModel.workflow_id == "case-review",
                        InvModel.case_id == case_id,
                        InvModel.status.notin_(["failed"]),
                    )
                    .first()
                )
                if existing:
                    logger.debug(
                        f"Case-review already exists for {case_id}: {existing.investigation_id}"
                    )
                    return

            case_data = None
            if self._data_service:
                case_data = self._data_service.get_case(case_id)
            if not case_data:
                logger.warning(f"Case {case_id} not found, skipping case review")
                return

            case_title = case_data.get("title", case_id)
            finding_ids = case_data.get("finding_ids", [])
            priority = case_data.get("priority", "medium")

            inv_id = f"inv-{utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
            total_steps = count_steps("case-review")

            workdir = self.workdir.create(inv_id)

            plan_md = generate_case_review_plan(
                inv_id, case_id, case_title, finding_ids, priority
            )
            self.workdir.write_file(inv_id, "plan.md", plan_md)

            state = generate_initial_state(
                inv_id, "case-review", case_id, [], total_steps
            )
            self.workdir.write_state(inv_id, state)

            context_md = generate_case_review_context(case_id, case_title, finding_ids)
            self.workdir.write_file(inv_id, "context.md", context_md)

            inv_record = {
                "investigation_id": inv_id,
                "case_id": case_id,
                "workflow_id": "case-review",
                "trigger_type": "case_review",
                "trigger_ids": finding_ids[:10],
                "status": "assigned",
                "workdir": str(workdir),
                "current_step": 1,
                "total_steps": total_steps,
                "priority": priority,
                "max_iterations": self.config.max_iterations_per_agent,
                "max_cost_usd": self.config.max_cost_per_investigation,
                "max_runtime_seconds": self.config.max_runtime_per_investigation,
            }

            self._save_investigation(inv_record)

            self.workdir.append_log(
                inv_id,
                {
                    "event": "investigation_created",
                    "workflow_id": "case-review",
                    "trigger_type": "case_review",
                    "case_id": case_id,
                },
            )

            logger.info(
                f"Created case-review investigation {inv_id} for case {case_id}"
            )

        except Exception as e:
            logger.error(
                f"Failed to trigger case review for {case_id}: {e}", exc_info=True
            )

    # -------------------------------------------------------------------------
    # AI Decision Logging
    # -------------------------------------------------------------------------

    def _log_ai_decision(
        self,
        decision_type: str,
        inv_id: str,
        reasoning: str,
        action: str,
        confidence: float = 1.0,
    ):
        """Log a master agent decision to the AIDecisionLog table."""
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import AIDecisionLog, Investigation

            with get_db_manager().session_scope() as session:
                case_id = None
                finding_id = None
                inv = (
                    session.query(Investigation)
                    .filter_by(investigation_id=inv_id)
                    .first()
                )
                if inv:
                    case_id = inv.case_id
                    trigger_ids = inv.trigger_ids or []
                    if trigger_ids:
                        finding_id = (
                            trigger_ids[0] if isinstance(trigger_ids, list) else None
                        )

                entry = AIDecisionLog(
                    decision_id=f"orch-{uuid.uuid4().hex[:8]}",
                    agent_id=ORCHESTRATION_DECISION_ID,
                    workflow_id=inv_id,
                    finding_id=finding_id,
                    case_id=case_id,
                    decision_type=decision_type,
                    confidence_score=confidence,
                    reasoning=reasoning,
                    recommended_action=action,
                    decision_metadata={
                        "source": ORCHESTRATOR_ACTOR,
                        "investigation_id": inv_id,
                    },
                )
                session.add(entry)
            logger.debug(f"AI decision logged: {decision_type} for {inv_id}")
        except Exception as e:
            logger.error(f"Failed to log AI decision: {e}")

    # -------------------------------------------------------------------------
    # Cross-Investigation Correlation
    # -------------------------------------------------------------------------

    async def _check_cross_correlations(self, inv_id: str):
        """Detect and link investigations that share IOCs."""
        if not hasattr(self, "_linked_pairs"):
            self._linked_pairs: set = set()

        related = self.shared_intel.get_related_investigations(inv_id)
        if not related:
            return

        for other_id in related:
            pair_key = tuple(sorted([inv_id, other_id]))
            if pair_key in self._linked_pairs:
                continue
            self._linked_pairs.add(pair_key)

            shared_keys = self.shared_intel.get_shared_iocs(inv_id, other_id)
            logger.info(
                f"Cross-correlation: {inv_id} <-> {other_id} share {len(shared_keys)} IOCs: {shared_keys[:5]}"
            )

            inv_a = self.get_investigation(inv_id)
            inv_b = self.get_investigation(other_id)
            case_a = inv_a.get("case_id") if inv_a else None
            case_b = inv_b.get("case_id") if inv_b else None

            if case_a and case_b and case_a != case_b:
                try:
                    client = self._mcp_client
                    if client:
                        await client.call_tool(
                            "link_related_cases",
                            {
                                "case_id": case_a,
                                "related_case_id": case_b,
                                "relationship": "shared_iocs",
                            },
                        )
                        logger.info(f"Linked cases {case_a} <-> {case_b}")
                except Exception as e:
                    logger.debug(f"Failed to link cases: {e}")

            cross_note = (
                f"\n\n## Cross-Investigation Note\n"
                f"Related investigation {other_id} shares IOCs: {', '.join(shared_keys[:10])}. "
                f"Review for campaign correlation.\n"
            )
            other_note = (
                f"\n\n## Cross-Investigation Note\n"
                f"Related investigation {inv_id} shares IOCs: {', '.join(shared_keys[:10])}. "
                f"Review for campaign correlation.\n"
            )

            try:
                plan_a = self.workdir.read_file(inv_id, "plan.md")
                if f"Related investigation {other_id}" not in plan_a:
                    self.workdir.append_file(inv_id, "plan.md", cross_note)
            except Exception:
                pass

            try:
                plan_b = self.workdir.read_file(other_id, "plan.md")
                if f"Related investigation {inv_id}" not in plan_b:
                    self.workdir.append_file(other_id, "plan.md", other_note)
            except Exception:
                pass

            self.workdir.append_log(
                inv_id,
                {
                    "event": "cross_correlation",
                    "related_investigation": other_id,
                    "shared_iocs": shared_keys[:20],
                },
            )
            self.workdir.append_log(
                other_id,
                {
                    "event": "cross_correlation",
                    "related_investigation": inv_id,
                    "shared_iocs": shared_keys[:20],
                },
            )

    # -------------------------------------------------------------------------
    # Notifications
    # -------------------------------------------------------------------------

    def _send_notification(
        self,
        inv_id: str,
        notification_type: str,
        title: str,
        message: str,
        priority: str = "normal",
    ):
        """Create a CaseNotification record for the investigation."""
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import CaseNotification, Investigation

            with get_db_manager().session_scope() as session:
                inv = (
                    session.query(Investigation)
                    .filter_by(investigation_id=inv_id)
                    .first()
                )
                case_id = inv.case_id if inv else None

                notif = CaseNotification(
                    case_id=case_id,
                    user_id="admin",
                    notification_type=notification_type,
                    title=title,
                    message=message,
                    delivery_channel="ui",
                    priority=priority,
                    notification_metadata={"investigation_id": inv_id},
                )
                session.add(notif)
            logger.debug(f"Notification created for {inv_id}: {notification_type}")
        except Exception as e:
            logger.error(f"Failed to create notification for {inv_id}: {e}")

    async def _send_slack_for_notification(
        self, title: str, message: str, severity: str = "high"
    ):
        """Optionally forward urgent notifications to Slack."""
        try:
            if get_settings().daemon_slack_enabled is not True:
                return

            import httpx

            from core.config import get_integration_config

            config = get_integration_config("slack")
            token = config.get("bot_token")
            channel = config.get("default_channel", "#soc-alerts")
            if not token:
                return

            color_map = {"critical": "#ff0000", "high": "#ff9900", "medium": "#ffcc00"}
            payload = {
                "channel": channel,
                "attachments": [
                    {
                        "color": color_map.get(severity, "#36a64f"),
                        "title": title,
                        "text": message,
                        "footer": "AI SOC Orchestrator",
                    }
                ],
            }
            await asyncio.to_thread(
                httpx.post,
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
                follow_redirects=True,
            )
        except Exception as e:
            logger.debug(f"Slack notification failed: {e}")

    # -------------------------------------------------------------------------
    # Database Helpers
    # -------------------------------------------------------------------------

    def _save_investigation(self, inv_record: Dict):
        """Save a new investigation record to the database."""
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import Investigation

            with get_db_manager().session_scope() as session:
                inv = Investigation(
                    investigation_id=inv_record["investigation_id"],
                    case_id=inv_record.get("case_id"),
                    workflow_id=inv_record["workflow_id"],
                    trigger_type=inv_record["trigger_type"],
                    trigger_ids=inv_record.get("trigger_ids", []),
                    status=inv_record.get("status", "queued"),
                    workdir=inv_record["workdir"],
                    current_step=inv_record.get("current_step", 0),
                    total_steps=inv_record.get("total_steps", 0),
                    priority=inv_record.get("priority", "medium"),
                    max_iterations=inv_record.get(
                        "max_iterations", self.config.max_iterations_per_agent
                    ),
                    max_cost_usd=inv_record.get(
                        "max_cost_usd", self.config.max_cost_per_investigation
                    ),
                    max_runtime_seconds=inv_record.get(
                        "max_runtime_seconds", self.config.max_runtime_per_investigation
                    ),
                )
                session.add(inv)
        except Exception as e:
            logger.error(f"Failed to save investigation to DB: {e}")

    def _get_investigations_by_status(self, status: str) -> List:
        """Query investigations by status from the database."""
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import Investigation

            with get_db_manager().session_scope() as session:
                from core.storage.schemas import InvestigationSchema

                results = session.query(Investigation).filter_by(status=status).all()
                return InvestigationSchema.dump_many(results)
        except Exception as e:
            logger.debug(f"DB query for status={status} failed: {e}")
            return []

    def get_all_investigations(self, status: Optional[str] = None) -> List[Dict]:
        """Get all investigations, optionally filtered by status."""
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import Investigation

            with get_db_manager().session_scope() as session:
                from core.storage.schemas import InvestigationSchema

                query = session.query(Investigation)
                if status:
                    query = query.filter_by(status=status)
                return InvestigationSchema.dump_many(
                    query.order_by(Investigation.created_at.desc()).all()
                )
        except Exception as e:
            logger.debug(f"DB query failed: {e}")
            return []

    def get_investigation(self, inv_id: str) -> Optional[Dict]:
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import Investigation

            with get_db_manager().session_scope() as session:
                inv = (
                    session.query(Investigation)
                    .filter_by(investigation_id=inv_id)
                    .first()
                )
                from core.storage.schemas import InvestigationSchema

                return InvestigationSchema.dump(inv) if inv else None
        except Exception:
            return None

    def _update_investigation_status(
        self, inv_id: str, status: str, notes: Optional[str] = None
    ):
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import Investigation

            with get_db_manager().session_scope() as session:
                inv = (
                    session.query(Investigation)
                    .filter_by(investigation_id=inv_id)
                    .first()
                )
                if inv:
                    inv.status = status
                    if notes:
                        inv.master_review_notes = notes
                    if status == "completed":
                        inv.completed_at = utcnow()
        except Exception as e:
            logger.error(f"Failed to update investigation status: {e}")

    def get_cost_summary(self) -> Dict[str, Any]:
        """Get cost breakdown across all investigations."""
        all_inv = self.get_all_investigations()
        total = sum(i.get("cost_usd", 0) for i in all_inv)
        active_cost = sum(
            i.get("cost_usd", 0)
            for i in all_inv
            if i.get("status") in ("assigned", "executing")
        )
        hourly = sum(c["cost"] for c in self._hourly_costs)
        return {
            "total_cost_usd": round(total, 4),
            "active_cost_usd": round(active_cost, 4),
            "hourly_cost_usd": round(hourly, 4),
            "hourly_budget_remaining": round(
                self.config.max_total_hourly_cost - hourly, 4
            ),
            "per_investigation_limit": self.config.max_cost_per_investigation,
        }

    async def purge_all_investigations(self) -> Dict[str, Any]:
        """Stop all running agents, delete every investigation row, and wipe
        the on-disk workdir tree. Used by the Settings UI's "Clear All
        Investigations" button as a hard reset for the auto-investigate
        subsystem.
        """
        self._abandon_in_flight()

        deleted = 0
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import Investigation

            with get_db_manager().session_scope() as session:
                deleted = session.query(Investigation).delete(synchronize_session=False)
        except Exception as e:
            logger.error(f"Failed to delete investigations from DB: {e}")
            raise

        try:
            base = self.workdir.base_dir
            if base.is_dir():
                import shutil

                shutil.rmtree(base, ignore_errors=True)
            base.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"Workdir cleanup failed after purge: {e}")

        self.shared_intel = SharedIntelligence()
        self._hourly_costs = []

        logger.warning(
            f"Purged {deleted} investigations and reset workdir tree at {self.workdir.base_dir}"
        )
        return {"deleted": deleted}

    # -------------------------------------------------------------------------
    # Utilities
    # -------------------------------------------------------------------------

    async def _sleep(self, shutdown_event: asyncio.Event, seconds: int):
        """Sleep that respects shutdown events."""
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
