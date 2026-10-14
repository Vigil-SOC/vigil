"""The Pulse reads: the day's spine, the gauges, work by workflow, and what waits.

NOT IMPLEMENTED YET. The console screen (``clients/web/src/screens/monitor/
PulseScreen.tsx``) is built and polls ``GET /api/pulse/{summary,throughput,work}``
every 15 s; until these reads are filled in the router answers 501 and each tab
shows its error state (demo mode serves ``fixtures`` instead). The response
contracts are ``PulseSummary``, ``PulseThroughput`` and ``PulseWork`` in
``core/monitor/models.py``: the routes validate these reads against them.
The Decisions, Sources and memory, and System tabs are placeholders in the
screen and call nothing yet.

Reference implementation: ``core/monitor/pulse.py`` on krmayankb/vigil
``fleet/console-monitor`` (PR #7). The TODOs note what to do differently.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from core.monitor.common import NotImplementedYet

HOURS = 24
# Age buckets for waiting work: upper bounds in minutes, None for the last.
AGE_BUCKETS = (
    (15, "under 15 min"),
    (60, "15 min to 1 h"),
    (240, "1 to 4 h"),
    (1440, "4 h to 1 day"),
    (None, "over 1 day"),
)


def spine(now: Optional[datetime] = None) -> dict:
    """Build ``PulseDay``: 24 hourly buckets (UTC) ending at ``now``.

    hours: ISO hour starts. alerts_in: findings created per hour.
    cases_formed: cases created. attached: intake rows merged into open cases.
    closed_by_agents / closed_by_person: ``case_closure_info`` by closer.
    running: workflow runs active at any point in the hour.
    queued: intake rows waiting at the end of each hour.
    decisions: approvals answered by a person.
    cap / high_water: the concurrency cap and intake high-water mark, or None.
    peak: ``{running, at, free}``. dropped / approved_by_person: unmeasured.
    info / queue_info: the ⓘ text for the two charts.

    TODO: do the hourly bucketing in SQL (``date_trunc('hour', ...)`` with
    ``GROUP BY``). PR #7 pulls every finding of the last 24 h into Python on
    every poll, and ``findings.created_at`` has no index (only ``timestamp``).
    TODO: a run with ``finished_at`` NULL that is no longer live (crashed,
    abandoned) must not count as running forever; bound it by its lease or
    last activity.
    """
    raise NotImplementedYet("Pulse day read is not implemented yet")


def summary(now: Optional[datetime] = None) -> dict:
    """Build ``PulseSummary``: gauges, attention, day (``spine()``), adjustments.

    gauges, in this order, each ``metric(..., key=, label=)``:
      without_person: share of today's closes made by agents, ``sample=n``.
      closed_today: count, ``yesterday=`` count by this time yesterday.
      time_to_decide: median minutes from asked to answered,
        ``approval_actions`` answered today, ``sample=n``.
      spend_today: AI spend today against the daily cap.
        TODO: pick one source of truth for spend. PR #7 sums
        ``workflow_runs.total_cost_usd`` while Health shows LLM spend from
        another source, and PR #2's budget lives in Redis reservations. If
        PR #2 lands, show the number that actually blocks runs.
    attention: ``[{level, title, why, action, door}]`` derived only from what
        the reads can see: decisions waiting, slots/spend/intake >= 75% of
        their limit. Never invented.
    adjustments: ``[{key, label, current, note}]`` for the fleet settings,
        read-only (changing them from Pulse is not built).
    """
    raise NotImplementedYet("Pulse summary read is not implemented yet")


def throughput(now: Optional[datetime] = None) -> dict:
    """Build ``PulseThroughput``: ``{now, day: spine(), kinds, info}``.

    kinds: the Fleet Wall bands without their cells (see ``wall.wall_payload``).
    """
    raise NotImplementedYet("Pulse throughput read is not implemented yet")


def work(now: Optional[datetime] = None) -> dict:
    """Build ``PulseWork``: ``{now, rows, queue, info}``.

    rows, each ``{key, label, what, count (metric), ages, oldest}``:
      needs_you: decisions waiting, aged from when they were asked.
      paused: cases whose run is paused, aged from their last activity.
      stuck, on_hold: unmeasured with a reason until they are recorded.
      TODO: add a "deferred" row for runs held back by a provider outage.
    ages: counts per ``AGE_BUCKETS``. oldest: ``{minutes, case_id}`` or None.
    queue: ``{hours, queued, high_water, queue_info}`` from ``spine()``.
    """
    raise NotImplementedYet("Pulse work read is not implemented yet")
