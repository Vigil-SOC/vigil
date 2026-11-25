"""Where suggestions live, and what people did with them.

A check makes a suggestion with :func:`propose`; the page reads them with
:func:`read`; a person dismisses one with :func:`dismiss`. History is
append-only, one row per apply, undo or dismissal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select, text, update
from sqlalchemy.orm import Session

from core.storage.models.supervisory import SupervisoryHistory, SupervisorySuggestion
from core.storage.unit_of_work import unit_of_work

# The reasons the page offers. Anything else is refused.
DISMISS_REASONS = ("Too risky", "Evidence looks wrong", "Not now", "Already handled")
FUNCTIONS = ("triage", "investigation", "response")
COLUMN_KINDS = ("text", "mono", "case", "action")
EFFECTS = ("better", "worse", "same")
# A dismissal for these reasons stands for good; any other for RESUGGEST_AFTER.
STANDING_REASONS = ("Too risky", "Evidence looks wrong")
RESUGGEST_AFTER = timedelta(days=30)
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
    (same setting, same from and to values, see ``same_value``) and the
    dismissal still stands:
    "Too risky" and "Evidence looks wrong" stand for good, the other reasons
    for ``RESUGGEST_AFTER``. A malformed card or incident raises ``TypeError``.
    """
    _check_card(card)
    _check_incident(held_incident)
    if fn not in FUNCTIONS:
        raise TypeError(f"fn must be one of {', '.join(FUNCTIONS)}")
    if not isinstance(verb, str) or not verb:
        raise TypeError("verb must be text")
    from_value, to_value = str(from_value).strip(), str(to_value).strip()
    with unit_of_work() as session:
        _lock(session, key)
        if _still_dismissed(session, key, from_value, to_value):
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


def same_value(a: str, b: str) -> bool:
    """Whether two spellings name one value: "8", "8.0" and "8.00" do; "8" and "9" don't.

    Finite numbers compare exactly by value; anything else as stripped text.
    """
    try:
        x, y = Decimal(a.strip()), Decimal(b.strip())
    except (InvalidOperation, AttributeError):
        return str(a).strip() == str(b).strip()
    if not (x.is_finite() and y.is_finite()):
        return str(a).strip() == str(b).strip()
    return x == y


def _lock(session: Session, key: str) -> None:
    """Serialise writes to one setting's suggestions for this transaction."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"supervisory:{key}"},
    )


def _still_dismissed(
    session: Session, key: str, from_value: str, to_value: str
) -> bool:
    since = datetime.now(UTC) - RESUGGEST_AFTER
    standing = session.execute(
        select(SupervisoryHistory.from_value, SupervisoryHistory.to_value).where(
            SupervisoryHistory.setting_key == key,
            SupervisoryHistory.outcome == "dismissed",
            or_(
                SupervisoryHistory.note.in_(STANDING_REASONS),
                SupervisoryHistory.at >= since,
            ),
        )
    )
    return any(
        same_value(f, from_value) and same_value(t, to_value) for f, t in standing
    )


def _check_card(card: dict[str, Any]) -> None:
    for name in ("why", "evidence", "replay", "plain_words", "replay_title"):
        if not isinstance(card.get(name), str):
            raise TypeError(f"card.{name} must be text")
    table = card.get("table")
    if not isinstance(table, dict) or not all(
        isinstance(table.get(k), t)
        for k, t in (("title", str), ("columns", list), ("rows", list), ("foot", str))
    ):
        raise TypeError("card.table needs title, columns, rows and foot")
    if not all(
        isinstance(c, dict)
        and isinstance(c.get("label"), str)
        and c.get("kind") in COLUMN_KINDS
        for c in table["columns"]
    ):
        raise TypeError("card.table.columns need label and a known kind")
    if not all(
        isinstance(r, list)
        and all(isinstance(c, dict) and isinstance(c.get("text"), str) for c in r)
        for r in table["rows"]
    ):
        raise TypeError("card.table.rows are lists of cells with text")
    rows = card.get("replay_rows")
    if not isinstance(rows, list) or not all(
        isinstance(r, dict)
        and all(isinstance(r.get(k), str) for k in ("label", "now", "next"))
        and r.get("effect") in EFFECTS
        for r in rows
    ):
        raise TypeError("card.replay_rows need label, now, next and a known effect")
    check = card.get("check")
    if check is not None and not (
        isinstance(check, dict)
        and isinstance(check.get("passed"), bool)
        and isinstance(check.get("text"), str)
    ):
        raise TypeError("card.check needs passed and text, or is null")


def _check_incident(incident: dict[str, Any] | None) -> None:
    if incident is None:
        return
    if not all(
        isinstance(incident.get(k), str) for k in ("id", "title", "firing_since")
    ):
        raise TypeError("held_incident needs id, title and firing_since")


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
        # The setting's lock first, then the row, in the order propose takes them.
        key = session.execute(
            select(SupervisorySuggestion.setting_key).where(
                SupervisorySuggestion.suggestion_id == suggestion_id
            )
        ).scalar_one_or_none()
        if key is not None:
            _lock(session, key)
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
        entry["improved"] = h.improved
        entry["undoable"] = False
    return entry


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
