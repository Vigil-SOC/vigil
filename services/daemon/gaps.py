"""The detection gaps run: queued by the daemon's daily tick, or on demand by a person
over ``POST /api/detection-rules/gaps-run`` or ``start_gaps_run`` on /mcp (#4738).

One at a time: a start is refused while another gaps run is unfinished, and the
read_raised … write_raised span holds an advisory lock so two starts can't raise the
same evidence twice.
"""

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy import func, select

from core.agents.enablement import disabled_agent_ids
from core.detections import gap_run
from core.detections.engineering import proposals
from core.detections.tools import get_security_detection_tools
from core.storage.connection import get_db_manager
from core.workflows.enablement import is_enabled
from core.workflows.routing import DETECTION_GAPS_WORKFLOW

logger = logging.getLogger(__name__)

GAPS_RUN_LOCK_KEY = 0x7669_6769_6C47_4150  # "vigilGAP"

QUEUED, NOTHING_TO_RAISE, BLIND_SPOTS_ONLY, REFUSED, NOT_QUEUED = (
    "queued",
    "nothing_to_raise",
    "blind_spots_only",
    "refused",
    "not_queued",
)

# The gaps workflow is one agent alone, switched with the agent (the workflows
# route refuses agent-run- ids); the console's words for that toggle.
_GAPS_AGENT = DETECTION_GAPS_WORKFLOW.removeprefix("agent-run-")
WORKFLOW_OFF = (
    f"The detection gaps run is turned off: its workflow {DETECTION_GAPS_WORKFLOW} "
    "is off. Turn it on in Workflows → Agents with the 'MITRE ATT&CK analyst on' "
    "toggle."
)

_HYPOTHESIS = (
    "Draft a detection rule for each detection gap below that you judge "
    "real, check it with check_detection_candidate and save it with "
    "save_detection_rule. Each gap is tracked as a detection proposal and "
    "the rules you save attach to it; open no Case. The save's gap_replays "
    "shows whether the rule fired on the attack the gap's hunts proved: if "
    "not, revise and save again."
)


@dataclass
class GapsRunOutcome:
    """What a start did. ``status`` is one of QUEUED, NOTHING_TO_RAISE,
    BLIND_SPOTS_ONLY, REFUSED or NOT_QUEUED; ``message`` says it in words."""

    status: str
    message: str
    proposal_ids: List[str] = field(default_factory=list)
    intake_id: Optional[int] = None
    floor: Optional[int] = None
    blind_spots: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, [])}


@contextmanager
def _try_lock() -> Iterator[bool]:
    """Session advisory lock for one start; yields False when another holds it."""
    engine = get_db_manager().engine
    if engine is None:
        raise RuntimeError("Database not initialized")
    with engine.connect() as conn:
        got = bool(
            conn.execute(select(func.pg_try_advisory_lock(GAPS_RUN_LOCK_KEY))).scalar()
        )
        conn.commit()
        try:
            yield got
        finally:
            if got:
                conn.execute(select(func.pg_advisory_unlock(GAPS_RUN_LOCK_KEY)))
                conn.commit()


async def start_gaps_run(person: Optional[Dict[str, str]] = None) -> GapsRunOutcome:
    """Queue a mitre_analyst run on the gaps identify_gaps lists over the last
    WINDOW_DAYS, counting only evidence no earlier run raised, at or above the
    floor and not on a technique a rule is already proposed or in shadow for
    (proposals.worked_techniques), biggest first; none when there are none.
    With OpenCTI configured, the techniques its newest intrusion sets and
    reports name are checked here too, in code, and each one with no proven rule
    is a gap raised once.

    The run opens no Case: each raised gap a rule can answer is a detection
    proposal holding the gap and its evidence ids, the run carries their ids in
    its trigger_context, and the rules it saves are attached to them. A blind
    spot is a missing data source no rule answers, shown on the Detections page:
    it is never handed to the run, and is reported as new the day it is raised (it
    is remembered as raised while it stays open), so a start that raises only blind
    spots, or nothing, queues nothing. What was raised is remembered
    only once the run has queued, or found nothing to queue.

    ``person`` is ``started_by`` and ``principal`` of whoever started it (the
    orchestrator's ``_person``); with one the row is ``trigger_type`` "manual", so
    the daily clock doesn't count it. None is the daily tick.
    """
    if not is_enabled(DETECTION_GAPS_WORKFLOW) or _GAPS_AGENT in disabled_agent_ids():
        return GapsRunOutcome(REFUSED, WORKFLOW_OFF)
    with _try_lock() as got:
        if not got:
            return GapsRunOutcome(
                REFUSED, "Another detection gaps run is being started right now."
            )
        # Inside the lock, so two starts can't both find none.
        unfinished = proposals.unfinished_gap_run()
        if unfinished is not None:
            intake_id, investigation_id = unfinished
            which = (
                f"investigation {investigation_id}"
                if investigation_id
                else "still queued"
            )
            return GapsRunOutcome(
                REFUSED,
                f"A detection gaps run is unfinished (intake row {intake_id}, "
                f"{which}); start another once it ends.",
                intake_id=intake_id,
            )
        return await _raise_and_queue(person)


async def _raise_and_queue(person: Optional[Dict[str, str]]) -> GapsRunOutcome:
    from core.storage.intake_queue import insert_intake_trigger

    started = datetime.now(timezone.utc)
    window_start = started - timedelta(days=gap_run.WINDOW_DAYS)
    before = gap_run.read_raised()
    intel = await gap_run.recent_intel()
    # Off the loop: the report reads the ledger and Findings synchronously.
    report = await asyncio.to_thread(
        asyncio.run,
        get_security_detection_tools().identify_gaps(
            since_days=gap_run.WINDOW_DAYS,
            since=window_start,
            techniques=sorted(intel) or None,
        ),
    )
    alerts = await asyncio.to_thread(gap_run.alert_ids, window_start)
    gaps = gap_run.gaps_in(report, alerts, before, intel)
    worked = await asyncio.to_thread(proposals.worked_techniques)
    raised, held = gap_run.to_raise(gaps, before, worked, window_start=window_start)
    after = gap_run.after_run(before, started, window_start, gaps, raised, held)
    to_draft = gap_run.rule_gaps(raised)
    blind = [g.line() for g in raised if g.kind == gap_run.BLIND]
    if not to_draft:
        gap_run.write_raised(after)
        if blind:
            return GapsRunOutcome(
                BLIND_SPOTS_ONLY,
                "Only new blind spots, which no rule answers; nothing queued.",
                floor=gap_run.GAP_FLOOR,
                blind_spots=blind,
            )
        return GapsRunOutcome(
            NOTHING_TO_RAISE,
            f"No new gaps a rule can answer at or above {gap_run.GAP_FLOOR}; "
            "nothing queued.",
            floor=gap_run.GAP_FLOOR,
        )

    hypothesis = (
        _HYPOTHESIS
        + " New gaps, biggest first: "
        + "; ".join(g.line() for g in to_draft)
    )
    # Each gap a rule can answer, with its evidence, as a proposal the run's saves
    # attach to. A record that fails costs the record, not the run.
    try:
        proposal_ids = proposals.for_gaps(to_draft)
    except Exception:  # noqa: BLE001
        logger.exception("Detection gaps run: the gaps were not recorded")
        proposal_ids = []
    queued = None
    try:
        queued = insert_intake_trigger(
            kind="schedule",
            priority="low",
            payload={
                "workflow_id": DETECTION_GAPS_WORKFLOW,
                "trigger_type": "manual" if person else "scheduled",
                "finding_ids": [],
                "hypothesis": hypothesis,
                # The run's record is the run and its proposals, not a Case.
                "no_case": True,
                # Read off the run's trigger_context when it saves a rule; kept even
                # when empty, so the run counts as unfinished while it works.
                "gap_proposal_ids": proposal_ids,
                **(person or {}),
            },
        )
    finally:
        if queued is None and proposal_ids:
            try:
                proposals.withdraw_gaps(
                    proposal_ids,
                    "The gaps run was not queued; it raises this again.",
                )
            except Exception:  # noqa: BLE001 -- never hides why it was not queued
                logger.exception("Detection gaps run: drafts not withdrawn")
    if queued is None:
        # Nothing was queued: remember nothing, so the next run raises it again.
        return GapsRunOutcome(NOT_QUEUED, "The gaps run's intake row was not queued.")
    gap_run.write_raised(after)
    return GapsRunOutcome(
        QUEUED,
        hypothesis,
        proposal_ids=proposal_ids,
        intake_id=queued,
        floor=gap_run.GAP_FLOOR,
        blind_spots=blind,
    )
