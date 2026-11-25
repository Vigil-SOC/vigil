"""Where suggestions live, and what people did with them.

A check makes a suggestion with :func:`propose`; the page reads them with
:func:`read`; a person dismisses one with :func:`dismiss`. History is
append-only, one row per apply, undo or dismissal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from core.storage.models.supervisory import SupervisoryHistory, SupervisorySuggestion
from core.storage.unit_of_work import unit_of_work

# The reasons the page offers. Anything else is refused.
DISMISS_REASONS = ("Too risky", "Evidence looks wrong", "Not now", "Already handled")
LIVE = ("open", "held")
HISTORY_LIMIT = 100

# Apply and undo are not built yet, so no card offers them.
_NO_APPLY = {"possible": False, "reason": "One-click apply is not available yet"}

_CARD_FIELDS = (
    "why",
    "evidence",
    "replay",
    "check",
    "plain_words",
    "table",
    "replay_title",
    "replay_rows",
)


class StoreRefusal(Exception):
    """A request the store will not carry out, with the HTTP status that says why."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def propose(
    *,
    fn: str,
    key: str,
    verb: str,
    from_value: str,
    to_value: str,
    card: dict[str, Any],
    widens_autonomy: bool = False,
    held_incident: dict[str, Any] | None = None,
) -> str | None:
    """Store a suggestion, replacing the live one for the same setting.

    Returns None, storing nothing, when a person dismissed this same change
    (same setting, same target value) before.
    """
    with unit_of_work() as session:
        dismissed = session.execute(
            select(SupervisoryHistory.id).where(
                SupervisoryHistory.setting_key == key,
                SupervisoryHistory.to_value == to_value,
                SupervisoryHistory.outcome == "dismissed",
            )
        ).first()
        if dismissed is not None:
            return None
        session.execute(
            update(SupervisorySuggestion)
            .where(
                SupervisorySuggestion.setting_key == key,
                SupervisorySuggestion.state.in_(LIVE),
            )
            .values(state="superseded")
        )
        suggestion_id = str(uuid.uuid4())
        session.add(
            SupervisorySuggestion(
                suggestion_id=suggestion_id,
                fn=fn,
                setting_key=key,
                verb=verb,
                from_value=from_value,
                to_value=to_value,
                widens_autonomy=widens_autonomy,
                card={k: card.get(k) for k in _CARD_FIELDS},
                state="held" if held_incident else "open",
                held_incident=held_incident,
            )
        )
    return suggestion_id


def read() -> dict[str, list[dict[str, Any]]]:
    """Open suggestions, held ones, and the latest history, as the page reads them."""
    with unit_of_work() as session:
        live = (
            session.execute(
                select(SupervisorySuggestion)
                .where(SupervisorySuggestion.state.in_(LIVE))
                .order_by(SupervisorySuggestion.created_at.desc())
            )
            .scalars()
            .all()
        )
        history = (
            session.execute(
                select(SupervisoryHistory)
                .order_by(SupervisoryHistory.at.desc(), SupervisoryHistory.id.desc())
                .limit(HISTORY_LIMIT)
            )
            .scalars()
            .all()
        )
        return {
            "suggestions": [_suggestion(s) for s in live if s.state == "open"],
            "held": [_held(s) for s in live if s.state == "held"],
            "history": [_entry(h) for h in history],
        }


def dismiss(
    suggestion_id: str, reason: str, actor: str, principal_id: str | None
) -> dict[str, Any]:
    """Record a person dismissing a live suggestion, and why."""
    if reason not in DISMISS_REASONS:
        raise StoreRefusal(422, f"reason must be one of: {', '.join(DISMISS_REASONS)}")
    with unit_of_work() as session:
        row = session.execute(
            select(SupervisorySuggestion)
            .where(SupervisorySuggestion.suggestion_id == suggestion_id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise StoreRefusal(404, f"No suggestion {suggestion_id}")
        if row.state not in LIVE:
            raise StoreRefusal(409, f"Suggestion {suggestion_id} is {row.state}")
        row.state = "dismissed"
        entry = SupervisoryHistory(
            suggestion_id=row.suggestion_id,
            at=datetime.now(UTC),
            setting_key=row.setting_key,
            from_value=row.from_value,
            to_value=row.to_value,
            outcome="dismissed",
            note=reason,
            actor=actor,
            actor_principal_id=principal_id,
        )
        session.add(entry)
        session.flush()
        return _entry(entry)


def _suggestion(s: SupervisorySuggestion) -> dict[str, Any]:
    card = s.card or {}
    return {
        "id": s.suggestion_id,
        "fn": s.fn,
        "verb": s.verb,
        "key": s.setting_key,
        "from": s.from_value,
        "to": s.to_value,
        "widens_autonomy": s.widens_autonomy,
        "suggested_at": _iso(s.created_at),
        **{k: card.get(k) for k in _CARD_FIELDS},
        # The 30-case check is for investigation suggestions only.
        "check": card.get("check") if s.fn == "investigation" else None,
        "apply": dict(_NO_APPLY),
    }


def _held(s: SupervisorySuggestion) -> dict[str, Any]:
    return {
        "id": s.suggestion_id,
        "fn": s.fn,
        "verb": s.verb,
        "key": s.setting_key,
        "from": s.from_value,
        "to": s.to_value,
        "incident": s.held_incident or {},
    }


def _entry(h: SupervisoryHistory) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": str(h.id),
        "at": _iso(h.at),
        "key": h.setting_key,
        "from": h.from_value,
        "to": h.to_value,
        "outcome": h.outcome,
        "note": h.note or "",
    }
    if h.outcome == "applied":
        entry["improved"] = None
        entry["undoable"] = False
    return entry


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
