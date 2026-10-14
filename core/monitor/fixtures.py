"""Sample Fleet Wall and Pulse payloads, served in demo mode.

They let the console and demos run before the reads in ``wall.py`` and
``pulse.py`` exist, and show an implementer what a full payload looks like.
Built from the contract models, so a fixture that drifts from the contract
fails to construct. Seeded, so the wall does not reshuffle on every poll; the
case ids are made up and do not open real cases.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.monitor.common import UNMEASURED, rate_level, usage_level
from core.monitor.models import (
    BandRate,
    BandRunning,
    FleetWallPayload,
    MonitorMetric,
    PulseAdjustment,
    PulseAge,
    PulseAttention,
    PulseDay,
    PulseGauge,
    PulseOldest,
    PulsePeak,
    PulseQueue,
    PulseSummary,
    PulseThroughput,
    PulseWork,
    PulseWorkRow,
    WallBand,
    WallCell,
    WallClosedToday,
    WallDecision,
    WallFacet,
    WallInfo,
    WallJustClosed,
    WallNextAction,
    WallSlots,
)
from core.monitor.pulse import AGE_BUCKETS, HOURS
from core.monitor.wall import COMBINED_HEAVY, JUST_CLOSED_MINUTES

INFO = "Sample data: demo mode is on."
FLEET_CAP = 80
HIGH_WATER = 500
DAILY_SPEND_CAP = 100.0

# (workflow_id, name, open cases); unassigned last.
WORKFLOWS = (
    ("full-investigation", "Full Investigation", 34),
    ("phishing-triage", "Phishing Triage", 22),
    ("endpoint-containment", "Endpoint Containment", 9),
    ("cloud-misconfig", "Cloud Misconfiguration", 14),
    ("unassigned", "Unassigned", 5),
)
TITLES = (
    "Beaconing from ws-{n} to a rare domain",
    "Suspicious OAuth consent by user{n}",
    "Credential dumping on srv-{n}",
    "Phishing link clicked by user{n}",
    "Public storage bucket logs-{n}",
    "Impossible travel for user{n}",
    "Unusual PowerShell on ws-{n}",
    "New admin added to tenant-{n}",
)
# Closed without a person, per workflow: spread across good, fair and poor
# (common.rate_level) so the demo shows every level.
RATES = (0.97, 0.91, 0.88, 0.79, 0.62)
# State, share of cells, and the one-line reason; the order is the wall's order.
STATES = (
    ("needs_you", 0.06, "Isolate the host? Approval requested"),
    ("stuck", 0.04, "No explanation moved in 7 steps"),
    ("running", 0.60, "A run is working this case"),
    ("paused", 0.10, "Run paused awaiting enrichment"),
    ("on_hold", 0.04, "Held by an analyst"),
    ("waiting", 0.16, "Queued for a free slot"),
)
ORDER = {state: i for i, (state, _, _) in enumerate(STATES)}
REASON = {state: reason for state, _, reason in STATES}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _unmeasured(reason: str) -> MonitorMetric:
    return MonitorMetric(
        value=None, info=INFO, unmeasured_text=UNMEASURED, unmeasured_reason=reason
    )


def _state(rng: random.Random) -> str:
    roll, acc = rng.random(), 0.0
    for state, share, _ in STATES:
        acc += share
        if roll < acc:
            return state
    return "waiting"


def _cells(
    rng: random.Random, workflow_id: str, n: int, first: int, now: datetime
) -> list[WallCell]:
    cells = []
    for i in range(n):
        state = _state(rng)
        alerts = rng.choice((1, 1, 1, 2, 3, 6, 9))
        used = round(rng.betavariate(2, 4), 2)
        cells.append(
            WallCell(
                case_id=f"case-{first + i:04d}",
                title=rng.choice(TITLES).format(n=rng.randint(10, 99)),
                description=f"Sample case in {workflow_id}.",
                workflow_id=workflow_id,
                severity=rng.choice(("critical", "high", "medium", "low")),
                state=state,
                reason=REASON[state],
                alerts=alerts,
                combined=alerts > 1,
                heavy=alerts >= COMBINED_HEAVY,
                limit_used=used,
                limit_level=usage_level(used, 1),
                near_limit=used >= 0.75,
                steps=rng.randint(1, 14),
                owner=rng.choice((None, None, "analyst")),
                sla_seconds_left=rng.randint(-600, 14_400),
                decision=(
                    WallDecision(
                        title="Isolate the host",
                        reason="C2 beaconing confirmed",
                        since=_iso(now - timedelta(minutes=rng.randint(2, 90))),
                    )
                    if state == "needs_you"
                    else None
                ),
            )
        )
    cells.sort(key=lambda cell: ORDER[cell.state])
    return cells


def _bands(now: datetime, with_cells: bool = True) -> list[WallBand]:
    rng = random.Random(7)
    bands, first = [], 1
    for workflow_id, name, n in WORKFLOWS:
        cells = _cells(rng, workflow_id, n, first, now)
        first += n
        running = sum(cell.state == "running" for cell in cells)
        cap = max(running + 2, int(running * 1.4))
        rate = RATES[len(bands)]
        bands.append(
            WallBand(
                workflow_id=workflow_id,
                name=name,
                open=n,
                running=BandRunning(
                    value=running,
                    limit=cap,
                    level=usage_level(running, cap),
                    info=INFO,
                    cap_text=f"{cap} slots for this workflow",
                ),
                today=rng.randint(3, 40),
                cost_per_case=round(rng.uniform(0.1, 2.5), 2),
                mean_steps=round(rng.uniform(3, 11), 1),
                waiting=sum(cell.state == "waiting" for cell in cells),
                without_person=BandRate(
                    value=rate,
                    level=rate_level(rate),
                    info=INFO,
                    sample=rng.randint(5, 40),
                ),
                cells=cells if with_cells else None,
            )
        )
    return bands


def wall_payload(
    now: Optional[datetime] = None, just_closed_minutes: int = JUST_CLOSED_MINUTES
) -> FleetWallPayload:
    now = now or _now()
    bands = _bands(now)
    cells = [cell for band in bands for cell in band.cells or []]

    def count(state: str) -> int:
        return sum(cell.state == state for cell in cells)

    asking = [cell for cell in cells if cell.state == "needs_you"]
    running = count("running")
    near = sum(cell.near_limit for cell in cells)
    return FleetWallPayload(
        now=_iso(now),
        next_action=(
            WallNextAction(
                count=len(asking),
                case_id=asking[0].case_id,
                title=asking[0].title,
                reason=asking[0].reason,
            )
            if asking
            else None
        ),
        slots=WallSlots(
            value=running,
            limit=FLEET_CAP,
            level=usage_level(running, FLEET_CAP),
            info=INFO,
            near_limit=near,
        ),
        closed_today=WallClosedToday(
            value=57, info=INFO, yesterday=48, without_person=0.82
        ),
        incidents_today=_unmeasured("Incident declarations are not recorded yet."),
        load=MonitorMetric(
            value=37, limit=HIGH_WATER, level=usage_level(37, HIGH_WATER), info=INFO
        ),
        facets=[
            WallFacet(key="all", label="All open", count=len(cells)),
            WallFacet(key="needs_you", label="Needs you", count=count("needs_you")),
            WallFacet(key="stuck", label="Stuck", count=count("stuck")),
            WallFacet(key="near_limit", label="Near its limit", count=near),
            WallFacet(key="running", label="Running", count=running),
            WallFacet(key="paused", label="Paused", count=count("paused")),
            WallFacet(key="on_hold", label="On hold", count=count("on_hold")),
        ],
        bands=bands,
        omitted=0,
        just_closed=[
            WallJustClosed(
                case_id=f"case-9{i:03d}",
                closed_at=_iso(now - timedelta(minutes=2 + 3 * i)),
                by=by,
            )
            for i, by in enumerate(("agents", "a person", "agents", "agents"))
        ],
        just_closed_minutes=just_closed_minutes,
        info=WallInfo(
            cell="One square per open case, coloured by state. " + INFO,
            kind="One band per workflow. " + INFO,
        ),
    )


def _day(now: datetime) -> PulseDay:
    rng = random.Random(11)
    hour = now.replace(minute=0, second=0)
    hours = [_iso(hour - timedelta(hours=HOURS - 1 - i)) for i in range(HOURS)]

    def wave(base: float, amp: float) -> list[int]:
        return [
            max(0, int(base + amp * (1 + math.sin(i / 3.5)) + rng.randint(-2, 2)))
            for i in range(HOURS)
        ]

    running = [min(FLEET_CAP, n) for n in wave(30, 22)]
    peak = max(range(HOURS), key=running.__getitem__)
    return PulseDay(
        hours=hours,
        alerts_in=wave(20, 25),
        cases_formed=wave(3, 4),
        attached=wave(1, 3),
        closed_by_agents=wave(2, 3),
        closed_by_person=[rng.randint(0, 2) for _ in range(HOURS)],
        running=running,
        queued=wave(10, 20),
        decisions=[rng.randint(0, 3) for _ in range(HOURS)],
        cap=FLEET_CAP,
        high_water=HIGH_WATER,
        peak=PulsePeak(
            running=running[peak], at=hours[peak], free=FLEET_CAP - running[peak]
        ),
        dropped=_unmeasured("There is no drop line yet."),
        approved_by_person=_unmeasured("Approved closes are not recorded apart yet."),
        info="Per hour, last 24 h (UTC). " + INFO,
        queue_info="Intake rows queued at the end of each hour. " + INFO,
    )


def summary(now: Optional[datetime] = None) -> PulseSummary:
    now = now or _now()
    spend = 63.4
    return PulseSummary(
        now=_iso(now),
        gauges=[
            PulseGauge(
                value=0.82,
                level=rate_level(0.82),
                info=INFO,
                key="without_person",
                label="Handled without a person",
                sample=57,
            ),
            PulseGauge(
                value=57,
                info=INFO,
                key="closed_today",
                label="Cases closed today",
                yesterday=48,
            ),
            PulseGauge(
                value=7,
                info=INFO,
                key="time_to_decide",
                label="Median time to decide",
                sample=11,
            ),
            PulseGauge(
                value=spend,
                limit=DAILY_SPEND_CAP,
                level=usage_level(spend, DAILY_SPEND_CAP),
                info=INFO,
                key="spend_today",
                label="AI spend today",
            ),
        ],
        attention=[
            PulseAttention(
                level="poor",
                title="3 decisions waiting on a person",
                why="The oldest was asked 84 min ago",
                action="Open Needs you",
                door="needs_you",
            ),
            PulseAttention(
                level="fair",
                title="6 cases paused over 1 h",
                why="Waiting on enrichment",
                action="See paused and stuck",
                door="work",
            ),
        ],
        day=_day(now),
        adjustments=[
            PulseAdjustment(
                key="concurrency",
                label="Cases running at once · fleet",
                current=str(FLEET_CAP),
                note="Read here; change it in Settings.",
            ),
            PulseAdjustment(
                key="daily_cap",
                label="AI spend cap per day",
                current=f"${DAILY_SPEND_CAP:.0f}",
                note="Read here; change it in Settings.",
            ),
            PulseAdjustment(
                key="drop_line",
                label="Drop alerts scoring under",
                note="There is no drop line yet.",
                unmeasured_text=UNMEASURED,
            ),
        ],
    )


def throughput(now: Optional[datetime] = None) -> PulseThroughput:
    now = now or _now()
    return PulseThroughput(
        now=_iso(now),
        day=_day(now),
        kinds=_bands(now, with_cells=False),
        info="Per workflow, today. " + INFO,
    )


def _ages(*counts: int) -> list[PulseAge]:
    return [
        PulseAge(label=label, count=n) for (_, label), n in zip(AGE_BUCKETS, counts)
    ]


def work(now: Optional[datetime] = None) -> PulseWork:
    now = now or _now()
    day = _day(now)
    return PulseWork(
        now=_iso(now),
        rows=[
            PulseWorkRow(
                key="needs_you",
                label="Needs you",
                what="A decision waits on a person",
                count=MonitorMetric(value=3, info=INFO),
                ages=_ages(1, 1, 1, 0, 0),
                oldest=PulseOldest(minutes=84, case_id="case-0001"),
            ),
            PulseWorkRow(
                key="paused",
                label="Paused",
                what="A run paused and has not resumed",
                count=MonitorMetric(value=13, info=INFO),
                ages=_ages(2, 3, 5, 2, 1),
                oldest=PulseOldest(minutes=1630, case_id="case-0010"),
            ),
            PulseWorkRow(
                key="stuck",
                label="Stuck",
                what="No explanation moved in 6 or more steps",
                count=MonitorMetric(value=4, info=INFO),
                ages=_ages(0, 2, 1, 1, 0),
                oldest=PulseOldest(minutes=410, case_id="case-0002"),
            ),
            PulseWorkRow(
                key="on_hold",
                label="On hold",
                what="Held by a person",
                count=MonitorMetric(value=5, info=INFO),
                ages=_ages(0, 1, 2, 2, 0),
                oldest=PulseOldest(minutes=720, case_id="case-0030"),
            ),
        ],
        queue=PulseQueue(
            hours=day.hours,
            queued=day.queued,
            high_water=HIGH_WATER,
            queue_info=day.queue_info,
        ),
        info="How long each has waited. " + INFO,
    )
