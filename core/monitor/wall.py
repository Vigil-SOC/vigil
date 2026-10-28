"""The Fleet Wall read: every open case as a cell, grouped by workflow.

NOT IMPLEMENTED YET. The console screen (``clients/web/src/screens/monitor/
FleetWallScreen.tsx``) is built and polls ``GET /api/fleet-wall`` every 10 s;
until this read is filled in the router answers 501 and the screen shows its
error state (demo mode serves ``fixtures.wall_payload`` instead). The
response contract is ``FleetWallPayload`` in ``core/monitor/models.py``: the
route validates this read against it and the console's types are generated
from it.

Reference implementation: ``core/monitor/wall.py`` on krmayankb/vigil
``fleet/console-monitor`` (PR #7). Take its shape, not all of its sources: the
TODOs below note where that version reads a look-alike number instead of the
thing that is enforced, or reads it in a way that will not scale.

Every number goes through ``common.metric()``. A number Vigil does not record
yet is sent as unmeasured with a reason, never as a guessed zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from core.monitor.common import NotImplementedYet

# Open cases drawn on the wall; past this the payload says how many it left out.
WALL_LIMIT = 2_000
# How long a closed case stays in the just-closed strip.
JUST_CLOSED_MINUTES = 15
# A thick outline from this many alerts combined into one case.
COMBINED_HEAVY = 5
STATES = ("needs_you", "stuck", "running", "paused", "on_hold", "waiting")


def wall_payload(
    now: Optional[datetime] = None, just_closed_minutes: int = JUST_CLOSED_MINUTES
) -> dict:
    """Build ``FleetWallPayload``. Field by field:

    next_action: ``{count, case_id, title, reason}`` of decisions waiting on a
        person, or None. Source: ``core.response.approval_service.needs_you()``
        (the same data Home's "Needs you" reads).

    slots: ``metric(running, limit=cap, level=usage_level(...), near_limit=n)``.
        TODO: decide what "slots" means before wiring it. PR #7 counts open cases
        whose run is executing/assigned against ``orchestrator_max_agents``
        (default 3). If the fleet limits from PR #2 land (Redis model/tool gate,
        human lane), read those from the agent service's ``/fleet`` instead, or
        rename this gauge so it isn't mistaken for them.
        ``near_limit``: open cases that have spent >= 75% of ``max_cost_usd``.

    closed_today: ``metric(n, yesterday=m, without_person=share)``. Source:
        ``case_closure_info`` since midnight UTC vs yesterday up to this time.
        TODO: a closure with ``closed_by_kind`` NULL must not count as an agent
        close (PR #7 does ``closed_by_kind or "agent"``, which inflates
        "without a person").

    incidents_today: unmeasured until incident declarations are recorded.

    load: ``metric(queued, limit=high_water, level=...)``. Source:
        ``intake_triggers`` with ``state = 'queued'``. The high-water mark is
        PR #2's ``ORCHESTRATOR_INTAKE_HIGH_WATER``; leave ``limit`` None until a
        setting exists.

    facets: ``[{key, label, count, unmeasured_reason?}]``, in the design's
        order: all, needs_you, stuck, near_limit, running, paused, on_hold.
        ``stuck`` and ``on_hold`` take ``count`` None and a reason until Vigil
        records them. Cells use the same keys as their ``state`` (``WallState``
        in models.py: needs_you, running, paused, waiting, stuck, on_hold).
        TODO: add a "deferred" state for runs held back by a provider outage.
        Without it they read as running and the wall looks healthy while
        nothing moves.

    bands: one per workflow, unassigned last: ``{workflow_id, name, open,
        running (metric, cap_text), today, cost_per_case, mean_steps, waiting,
        without_person (metric, sample), cells}``.
        Cells come from ``CaseRepository.queue(limit=WALL_LIMIT, ...)`` through
        ``core.cases.combined_state.queue_item`` so the wall and the Cases page
        cannot disagree. A cell is ``WallCell`` in models.py; state is needs_you
        (asking), running (executing/assigned), paused, else waiting.
        TODO: PR #7 finds a closed case's workflow by
        ``workflow_runs.trigger_context->>'case_id'``, an unindexed JSON scan on
        every poll. Index it or store ``case_id`` as a column first.
        TODO: "cost_per_case" in PR #7 is the mean cost per *run* finished
        today. Compute it per case (sum of a case's runs), or rename it.
        ``waiting`` adds queued intake rows for the workflow; PR #3 records the
        chosen workflow in ``intake_triggers.triage_decision->>'workflow_id'``.

    omitted: open cases past WALL_LIMIT, not drawn.

    just_closed: ``[{case_id, closed_at, by}]`` closed in the last
        ``just_closed_minutes``, newest first; ``by`` is "agents" or
        "a person" (same NULL rule as closed_today).

    info: ``{cell, kind}``: the ⓘ text for the squares and the bands.
    """
    raise NotImplementedYet("Fleet Wall read is not implemented yet")
