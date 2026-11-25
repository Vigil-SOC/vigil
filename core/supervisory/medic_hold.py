"""Hold a suggestion while a related Medic incident is open.

A broken source or model makes results look worse for the wrong reason, so a
suggestion whose evidence an open Medic incident could distort waits, ``held``,
until that incident closes.

Which incidents hold which functions, by the family of Medic's rule: the part of
its rule id before the dot, as in ``llm.rate-limited``.

- ``ingest`` (sources, federation, Kafka, webhooks, threat intel) holds triage
  and investigation. What arrives, and what an investigation can search and
  enrich with, is what both are judged on. Response is judged on people's
  decisions on proposed actions, and a quiet source does not change those.
- ``llm`` (provider, gateway, keys, spend budget, rate limits, model) holds
  triage, investigation and response. All three run on the model.
- ``pipeline`` (agent containers and runs, daemon components, the processor,
  the orchestrator) holds triage, investigation and response. Its rules cover
  the processor and triage as well as the agent runs that make the proposals.
- Any other family, an incident with no rule (an unknown signature), and an
  incident about Medic's own watcher hold nothing.

Only an incident that is firing or resolving holds. An incident Medic has not
routed yet (``held`` in Medic's own words) or one a person suppressed does not.
When several hold the same function, the one firing longest holds it.

Medic is off unless ``VIGIL_MEDIC_ENABLED`` is on. While it is off nothing is
held: :func:`incident_for` holds nothing, and :func:`sweep` releases whatever was
held before. Until a sweep runs, a suggestion held before Medic was switched off
reads as held, and is counted as held. Medic is read through its own API,
as the incident notices read it: nothing here imports Medic or writes to it. A
read that does not come back changes nothing, so a held suggestion stays held
until Medic says the incident closed.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import func, select, text

from core.config import Settings, get_settings
from core.platform import medic_last_seen
from core.platform.medic_incidents import _INCIDENT, _TS, _clip, _pages, _when
from core.platform.medic_status import medic_enabled
from core.storage.connection import get_db_manager
from core.storage.models.supervisory import SupervisorySuggestion
from core.storage.unit_of_work import unit_of_work
from core.supervisory.store import LIVE, _lock

FAMILIES = {
    "ingest": ("triage", "investigation"),
    "llm": ("triage", "investigation", "response"),
    "pipeline": ("triage", "investigation", "response"),
}
_HOLDING_STATES = ("firing", "resolving")
_INCIDENT_FIELDS = ("id", "title", "firing_since")
# As long as a held card's line can usefully be; the incident notices clip the same way.
_TITLE_LIMIT = 200
# Two-key advisory locks never collide with the one-key per-setting locks.
_SWEEP_LOCK = (
    "SELECT pg_advisory_lock(hashtext('supervisory'), hashtext('medic-sweep'))"
)
_SWEEP_UNLOCK = (
    "SELECT pg_advisory_unlock(hashtext('supervisory'), hashtext('medic-sweep'))"
)


def open_incidents(settings: Settings | None = None) -> list[dict[str, Any]] | None:
    """Medic's open incidents that hold a function, firing longest first.

    Each is ``{id, title, firing_since, holds}``. Empty while Medic is off. None
    when Medic is on but could not be read, or has no address or key to read it by.
    """
    settings = settings or get_settings()
    if not medic_enabled(settings):
        return []
    url, key = settings.vigil_medic_api_url, medic_last_seen.read_key(settings)
    if not url or key is None:
        return None
    timeout = httpx.Timeout(medic_last_seen.REQUEST_TIMEOUT_S)
    with httpx.Client(
        timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        raw = _pages(client, url, key)
    if raw is None:
        return None
    by_id: dict[str, dict[str, Any]] = {}
    for item in (_incident(row) for row in raw):
        if item is not None:
            by_id.setdefault(item["id"], item)
    return sorted(by_id.values(), key=lambda i: (_when(i["firing_since"]), i["id"]))


def _incident(raw: Any) -> dict[str, Any] | None:
    """An incident that holds something, or None for one that holds nothing."""
    if not isinstance(raw, dict):
        return None
    iid, rule = raw.get("incident_id"), raw.get("rule")
    # Medic's text is untrusted: the id and time are checked for shape, as the
    # incident notices check them, and the title is clipped to one plain line.
    if not isinstance(iid, str) or not _INCIDENT.fullmatch(iid):
        return None
    if not isinstance(rule, dict):
        return None
    if raw.get("subject") != "vigil" or raw.get("state") not in _HOLDING_STATES:
        return None
    rule_id = rule.get("id")
    if not isinstance(rule_id, str):
        return None
    holds = FAMILIES.get(rule_id.partition(".")[0])
    if holds is None:
        return None
    since = raw.get("active_since")
    if not isinstance(since, str) or not _TS.fullmatch(since):
        since = raw.get("opened_at")
    if not isinstance(since, str) or not _TS.fullmatch(since) or _when(since) is None:
        return None
    title = _clip(raw.get("title"), _TITLE_LIMIT, one_line=True)
    return {
        "id": iid,
        "title": title or _clip(rule_id, _TITLE_LIMIT, one_line=True),
        "firing_since": since,
        "holds": holds,
    }


def incident_for(
    fn: str,
    incidents: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """The open incident that holds a suggestion for ``fn``, as the store keeps it.

    ``{id, title, firing_since}``, or None when nothing holds it. Reads Medic when
    ``incidents`` is not given; a read that does not come back holds nothing.
    """
    if incidents is None:
        incidents = open_incidents(settings) or []
    for incident in incidents:
        if fn in incident["holds"]:
            return {k: incident[k] for k in _INCIDENT_FIELDS}
    return None


def sweep(
    settings: Settings | None = None,
    incidents: list[dict[str, Any]] | None = None,
) -> dict[str, int] | None:
    """Hold each live suggestion an open incident is related to, and release the rest.

    One sweep at a time, across replicas: a second waits, then reads Medic afresh,
    so an older read never lands after a newer one. The wait is on its own
    connection, outside any transaction, so nothing is held open across the read.
    Each suggestion is moved under its setting's lock, the lock propose and dismiss
    take, and only while it is still live. Returns how many were held and released,
    or None, changing nothing, when Medic could not be read.

    ``incidents`` is a read of :func:`open_incidents` the caller already made, so
    that what it proposes next is held on the same read. The scheduled check passes
    one, read under its own one-at-a-time lock. Without it the sweep reads Medic
    itself, inside this lock.
    """
    manager = get_db_manager()
    manager.initialize()  # a no-op once the process has its engine
    with manager.engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as conn:
        conn.execute(text(_SWEEP_LOCK))
        try:
            if incidents is None:
                incidents = open_incidents(settings)
            return _sweep(incidents)
        finally:
            conn.execute(text(_SWEEP_UNLOCK))


def _sweep(incidents: list[dict[str, Any]] | None) -> dict[str, int] | None:
    if incidents is None:
        return None
    with unit_of_work() as session:
        live = session.execute(
            select(
                SupervisorySuggestion.suggestion_id,
                SupervisorySuggestion.setting_key,
                SupervisorySuggestion.fn,
            ).where(SupervisorySuggestion.state.in_(LIVE))
        ).all()
    held = released = 0
    for suggestion_id, key, fn in live:
        want = incident_for(fn, incidents)
        with unit_of_work() as session:
            _lock(session, key)
            row = session.execute(
                select(SupervisorySuggestion)
                .where(SupervisorySuggestion.suggestion_id == suggestion_id)
                .with_for_update()
            ).scalar_one_or_none()
            if row is None or row.state not in LIVE:
                continue
            if want is None:
                if row.state == "held":
                    row.state, row.held_incident = "open", None
                    released += 1
            elif row.state != "held" or row.held_incident != want:
                if row.state == "open":
                    held += 1
                row.state, row.held_incident = "held", want
    return {"held": held, "released": released}


def open_related() -> int:
    """How many Medic incidents are named on a held suggestion, as the last sweep left them.

    Only the incident firing longest is named for a function, so an incident that
    holds the same functions as an older one is not counted.
    """
    with unit_of_work() as session:
        return session.execute(
            select(
                func.count(
                    func.distinct(SupervisorySuggestion.held_incident["id"].astext)
                )
            ).where(SupervisorySuggestion.state == "held")
        ).scalar_one()
