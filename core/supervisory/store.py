"""Where suggestions live, and what people did with them.

A check makes a suggestion with :func:`propose`; the page reads them with
:func:`read`; a person dismisses one with :func:`dismiss`. History is
append-only, one row per apply, undo or dismissal.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from core.intent import FIELDS_BY_KEY, label
from core.response.config import applied_intent, managed_widening
from core.storage.models.supervisory import SupervisoryHistory, SupervisorySuggestion
from core.storage.unit_of_work import unit_of_work
from core.supervisory.settings import ACT_LOCKED, CEILING_UNREAD, STOPPED

# The reasons the page offers. Anything else is refused.
DISMISS_REASONS = ("Too risky", "Evidence looks wrong", "Not now", "Already handled")
FUNCTIONS = ("triage", "investigation", "response")
COLUMN_KINDS = ("text", "mono", "case", "action")
EFFECTS = ("better", "worse", "same")
# A dismissal, whatever its reason, or an undo holds the same change back this long.
RESUGGEST_AFTER = timedelta(days=30)
LIVE = ("open", "held")
HISTORY_LIMIT = 100

# Settings one click can change: the autonomy ceiling and the orchestrator settings.
APPLICABLE = frozenset(
    {
        "act.reversible",
        "act.irreversible",
        "act.four_eyes",
        "investigate.enabled",
        "investigate.max_cost_per_investigation",
        "investigate.max_total_hourly_cost",
        "investigate.max_runtime_per_investigation",
        "investigate.max_iterations_per_agent",
        "investigate.max_concurrent_agents",
    }
)
# The keys set_orchestrator_config checks against INTENT.md on every write.
_MANAGED_ORCHESTRATOR_KEYS = frozenset(
    {
        "investigate.enabled",
        "investigate.max_cost_per_investigation",
        "investigate.max_total_hourly_cost",
        "investigate.max_runtime_per_investigation",
    }
)
_NOT_APPLICABLE = "Vigil can't change this setting in one click"

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


# (setting key, target value) -> why the write path refuses that value itself, or None.
Validate = Callable[[str, str], "str | None"]


@dataclass(frozen=True)
class _Ctx:
    """What a page read checks cards and undos against."""

    current: dict[str, dict[str, Any]]
    applied: dict[str, Any]
    validate: Validate | None


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
    medic_unread: bool = False,
) -> str | None:
    """Store a suggestion, replacing the live one for the same setting.

    ``medic_unread``: Medic could not be read, so nothing is known about its
    incidents. A held suggestion this replaces passes its hold on, rather than
    the new one coming in open.

    Returns None, storing nothing, when a person dismissed or undid this same
    change (same setting, same from and to values, see ``same_value``) within
    ``RESUGGEST_AFTER``; after that the change can be suggested again if the
    data still supports it. A malformed card or incident raises ``TypeError``.
    """
    _check_card(card)
    _check_incident(held_incident)
    if not isinstance(key, str) or not key.strip():
        raise TypeError("key must be text")
    if not isinstance(widens_autonomy, bool):
        raise TypeError("widens_autonomy must be true or false")
    if fn not in FUNCTIONS:
        raise TypeError(f"fn must be one of {', '.join(FUNCTIONS)}")
    if not isinstance(verb, str) or not verb:
        raise TypeError("verb must be text")
    from_value, to_value = str(from_value).strip(), str(to_value).strip()
    with unit_of_work() as session:
        _lock(session, key)
        if _held_back(session, key, from_value, to_value):
            return None
        card = {k: card.get(k) for k in _CARD_FIELDS}
        if card.get("check") is None:
            card["check"] = _kept_check(session, key, from_value, to_value)
        if held_incident is None and medic_unread:
            held_incident = _kept_hold(session, key)
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
                card=card,
                state="held" if held_incident else "open",
                held_incident=held_incident,
            )
        )
    return suggestion_id


def _kept_hold(session: Session, key: str) -> dict[str, Any] | None:
    """The incident holding the live suggestion for ``key``; None when it is open."""
    return session.execute(
        select(SupervisorySuggestion.held_incident).where(
            SupervisorySuggestion.setting_key == key,
            SupervisorySuggestion.state == "held",
        )
    ).scalar_one_or_none()


def _kept_check(
    session: Session, key: str, from_value: str, to_value: str
) -> dict[str, Any] | None:
    """The 30-case check a person recorded on the live suggestion for this same
    change, so proposing it again does not drop it; None when there is none."""
    for live in session.execute(
        select(SupervisorySuggestion).where(
            SupervisorySuggestion.setting_key == key,
            SupervisorySuggestion.state.in_(LIVE),
        )
    ).scalars():
        check = (live.card or {}).get("check")
        if (
            check is not None
            and same_value(live.from_value, from_value)
            and same_value(live.to_value, to_value)
        ):
            return check
    return None


def live_change(key: str) -> tuple[str, str] | None:
    """The from and to values of the live suggestion for ``key``, or None."""
    with unit_of_work() as session:
        row = session.execute(
            select(
                SupervisorySuggestion.from_value, SupervisorySuggestion.to_value
            ).where(
                SupervisorySuggestion.setting_key == key,
                SupervisorySuggestion.state.in_(LIVE),
            )
        ).first()
    return (row[0], row[1]) if row else None


def record_check(
    key: str, check: dict[str, Any], from_value: str, to_value: str
) -> str:
    """Put a 30-case check on the live suggestion for ``key``, which must still
    propose ``from_value`` to ``to_value``. Returns its id; refuses otherwise."""
    if not (
        isinstance(check, dict)
        and isinstance(check.get("passed"), bool)
        and isinstance(check.get("text"), str)
    ):
        raise TypeError("check needs passed and text")
    with unit_of_work() as session:
        _lock(session, key)
        row = session.execute(
            select(SupervisorySuggestion)
            .where(
                SupervisorySuggestion.setting_key == key,
                SupervisorySuggestion.state.in_(LIVE),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise StoreRefusal(404, f"No live suggestion for {key}")
        if row.fn != "investigation":
            raise StoreRefusal(
                409, "The 30-case check is for investigation suggestions only"
            )
        if not (
            same_value(row.from_value, from_value)
            and same_value(row.to_value, to_value)
        ):
            raise StoreRefusal(
                409,
                f"The suggestion is {row.from_value} → {row.to_value}, "
                f"not {from_value} → {to_value}",
            )
        # A new dict, so the JSONB column is written rather than mutated in place.
        row.card = {**(row.card or {}), "check": check}
        return row.suggestion_id


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


def lock_orchestrator_row(session: Session) -> None:
    """Serialise one-click writes to the orchestrator settings row for this transaction.

    Applying any investigate.* key rewrites the whole row from the values read
    just before, so two applies on different keys must not overlap. Taken after
    the setting's own lock, by every caller, so the order never inverts.
    """
    _lock(session, "orchestrator.settings")


def _held_back(session: Session, key: str, from_value: str, to_value: str) -> bool:
    """A person dismissed or undid this same change within ``RESUGGEST_AFTER``."""
    since = datetime.now(UTC) - RESUGGEST_AFTER
    standing = session.execute(
        select(SupervisoryHistory.from_value, SupervisoryHistory.to_value).where(
            SupervisoryHistory.setting_key == key,
            SupervisoryHistory.outcome.in_(("dismissed", "undone")),
            SupervisoryHistory.at >= since,
        )
    )
    return any(
        same_value(f, from_value) and same_value(t, to_value) for f, t in standing
    )


def _check_card(card: dict[str, Any]) -> None:
    if not isinstance(card, dict):
        raise TypeError("card must be an object")
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
        and all(
            isinstance(c, dict)
            and isinstance(c.get("text"), str)
            and isinstance(c.get("attack", False), bool)
            for c in r
        )
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
    if not isinstance(incident, dict) or not all(
        isinstance(incident.get(k), str) for k in ("id", "title", "firing_since")
    ):
        raise TypeError("held_incident needs id, title and firing_since")


def read(
    current: dict[str, dict[str, Any]] | None = None,
    applied: dict[str, Any] | None = None,
    validate: Validate | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Open suggestions, held ones, and the latest history, as the page reads them.

    ``current`` is the settings list keyed by setting (value and change): it
    decides whether a card can be applied and whether an apply can be undone.
    Without it, nothing can. ``applied`` is the applied INTENT.md row the
    settings list was built from, read here when not given. ``validate`` says
    why the write path would refuse a value on its own terms (bounds, type),
    or None.
    """
    current = current or {}
    if applied is None:
        applied = applied_intent(fresh=True) if current else {}
    ctx = _Ctx(current, applied, validate)
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
        applied_ids = {h.suggestion_id for h in history if h.outcome == "applied"}
        states = (
            dict(
                session.execute(
                    select(
                        SupervisorySuggestion.suggestion_id, SupervisorySuggestion.state
                    ).where(SupervisorySuggestion.suggestion_id.in_(applied_ids))
                ).all()
            )
            if applied_ids
            else {}
        )
        latest = _latest_per_suggestion(session, applied_ids)
        return {
            "suggestions": [_suggestion(s, ctx) for s in live if s.state == "open"],
            "held": [_held(s) for s in live if s.state == "held"],
            "history": [_entry(h, _undoable(h, states, latest, ctx)) for h in history],
        }


def can_undo(
    key: str,
    applied_value: str,
    before: str,
    current: dict[str, dict[str, Any]],
    applied: dict[str, Any],
    validate: Validate | None = None,
) -> bool:
    """Whether an apply that set ``key`` to ``applied_value`` can be put back to
    ``before`` now: the setting still has the applied value and the write path
    would take the old one."""
    now = current.get(key)
    if now is None or not same_value(now["value"], applied_value):
        return False
    ctx = _Ctx(current, applied, validate)
    return _write_refusal(key, applied_value, before, ctx) is None


def locked_live(session: Session, suggestion_id: str) -> SupervisorySuggestion:
    """The open suggestion, locked with its setting for this transaction."""
    row = _locked(session, suggestion_id)
    if row.state != "open":
        raise StoreRefusal(409, f"Suggestion {suggestion_id} is {row.state}")
    return row


def locked_applied(
    session: Session, history_id: str
) -> tuple[SupervisoryHistory, SupervisorySuggestion]:
    """The apply to undo and its suggestion, locked with its setting.

    Only the latest record of a suggestion that is still applied can be undone.
    """
    try:
        entry_id = int(history_id)
    except ValueError:
        raise StoreRefusal(404, f"No history entry {history_id}") from None
    entry = session.get(SupervisoryHistory, entry_id)
    if entry is None:
        raise StoreRefusal(404, f"No history entry {history_id}")
    if entry.outcome != "applied":
        raise StoreRefusal(409, f"History entry {history_id} is not an apply")
    row = _locked(session, entry.suggestion_id)
    if row.state != "applied":
        raise StoreRefusal(409, f"That change is {row.state}, not applied")
    newest = _latest_per_suggestion(session, {row.suggestion_id}).get(row.suggestion_id)
    if newest != entry.id:
        raise StoreRefusal(409, "A later record replaced that apply")
    return entry, row


def record(
    session: Session,
    row: SupervisorySuggestion,
    outcome: str,
    actor: str,
    principal_id: str | None,
    note: str = "",
) -> dict[str, Any]:
    """Write one history row and move the suggestion to the matching state."""
    row.state = outcome
    entry = SupervisoryHistory(
        suggestion_id=row.suggestion_id,
        at=datetime.now(UTC),
        setting_key=row.setting_key,
        from_value=row.from_value,
        to_value=row.to_value,
        outcome=outcome,
        note=note,
        actor=actor,
        actor_principal_id=principal_id,
    )
    session.add(entry)
    session.flush()
    return _entry(entry, outcome == "applied")


def _locked(session: Session, suggestion_id: str) -> SupervisorySuggestion:
    """The suggestion, its setting's lock taken first, then its row, as propose does."""
    key = session.execute(
        select(SupervisorySuggestion.setting_key).where(
            SupervisorySuggestion.suggestion_id == suggestion_id
        )
    ).scalar_one_or_none()
    if key is None:
        raise StoreRefusal(404, f"No suggestion {suggestion_id}")
    _lock(session, key)
    return session.execute(
        select(SupervisorySuggestion)
        .where(SupervisorySuggestion.suggestion_id == suggestion_id)
        .with_for_update()
    ).scalar_one()


def _latest_per_suggestion(session: Session, ids: set[str]) -> dict[str, int]:
    if not ids:
        return {}
    return dict(
        session.execute(
            select(SupervisoryHistory.suggestion_id, func.max(SupervisoryHistory.id))
            .where(SupervisoryHistory.suggestion_id.in_(ids))
            .group_by(SupervisoryHistory.suggestion_id)
        ).all()
    )


def _undoable(
    h: SupervisoryHistory,
    states: dict[str, str],
    latest: dict[str, int],
    ctx: _Ctx,
) -> bool:
    """An apply can be put back while it is the suggestion's last record, still
    applied, and the setting still has the applied value."""
    if h.outcome != "applied" or states.get(h.suggestion_id) != "applied":
        return False
    if latest.get(h.suggestion_id) != h.id:
        return False
    now = ctx.current.get(h.setting_key)
    if now is None or not same_value(now["value"], h.to_value):
        return False
    refused = _write_refusal(h.setting_key, h.to_value, h.from_value, ctx)
    return refused is None


def dismiss(
    suggestion_id: str, reason: str, actor: str, principal_id: str | None
) -> dict[str, Any]:
    """Record a person dismissing a live suggestion, and why."""
    if reason not in DISMISS_REASONS:
        raise StoreRefusal(422, f"reason must be one of: {', '.join(DISMISS_REASONS)}")
    with unit_of_work() as session:
        row = _locked(session, suggestion_id)
        if row.state not in LIVE:
            raise StoreRefusal(409, f"Suggestion {suggestion_id} is {row.state}")
        return record(session, row, "dismissed", actor, principal_id, reason)


def _suggestion(s: SupervisorySuggestion, ctx: _Ctx) -> dict[str, Any]:
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
        "apply": _apply_state(s, ctx),
    }


def _apply_state(s: SupervisorySuggestion, ctx: _Ctx) -> dict[str, Any]:
    setting = ctx.current.get(s.setting_key)
    if s.setting_key not in APPLICABLE or setting is None:
        return {"possible": False, "reason": _NOT_APPLICABLE}
    change = setting.get("change") or {}
    if not change.get("possible"):
        return {"possible": False, "reason": change.get("reason") or _NOT_APPLICABLE}
    if not same_value(setting["value"], s.from_value):
        return {
            "possible": False,
            "reason": f"The setting is {setting['value']} now, not {s.from_value}",
        }
    refused = _write_refusal(s.setting_key, s.from_value, s.to_value, ctx)
    return {"possible": False, "reason": refused} if refused else {"possible": True}


def _write_refusal(key: str, from_value: str, to_value: str, ctx: _Ctx) -> str | None:
    """Why the write path would refuse changing ``key`` from ``from_value`` to
    ``to_value``, when the settings list can tell; None otherwise.

    The same checks the write paths make: a target wider than INTENT.md
    declares; an act flag loosened while autonomy is stopped; and, because an
    orchestrator write sends every managed investigate.* key, another of those
    keys already wider than declared.
    """
    if ctx.validate is not None:
        refused = ctx.validate(key, to_value)
        if refused:
            return refused
    refused = _past_declared(key, to_value, ctx.applied)
    if refused:
        return refused
    row = ctx.current.get(key) or {}
    reason = (row.get("change") or {}).get("reason")
    # The ceiling write refuses while the ceiling can't be read, and any raise
    # of an act flag while the environment forces them off.
    if key.startswith("act.") and reason == CEILING_UNREAD:
        return reason
    if reason == ACT_LOCKED and _loosens(key, from_value, to_value):
        return reason
    if (
        key.startswith("act.")
        and (row.get("change") or {}).get("reason") == STOPPED
        and _loosens(key, from_value, to_value)
    ):
        return STOPPED
    if key.startswith("investigate."):
        for other in _MANAGED_ORCHESTRATOR_KEYS - {key}:
            now = ctx.current.get(other)
            refused = now and _past_declared(other, now["value"], ctx.applied)
            if refused:
                return refused
    return None


def _loosens(key: str, from_value: str, to_value: str) -> bool:
    """Whether going from ``from_value`` to ``to_value`` loosens ``key``, by its
    INTENT.md tightening rule. False for a key without one."""
    field = FIELDS_BY_KEY.get(key)
    if field is None:
        return False

    try:
        # label(rule, new, old) reads as what moving from old to new does.
        return label(field.rule, _typed(to_value), _typed(from_value)) == "loosen"
    except (TypeError, ValueError):
        return False


def _past_declared(key: str, to_value: str, applied: dict[str, Any]) -> str | None:
    """Why a write of ``to_value`` would be refused as wider than INTENT.md
    declares for ``key``, or None. The same check the write paths make."""
    if key not in FIELDS_BY_KEY:
        return None
    try:
        return managed_widening(key, _typed(to_value), applied)
    except (TypeError, ValueError):
        return None


def _typed(value: str) -> Any:
    if value in ("on", "true"):
        return True
    if value in ("off", "false"):
        return False
    return float(value)


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


def _entry(h: SupervisoryHistory, undoable: bool = False) -> dict[str, Any]:
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
        entry["undoable"] = undoable
    return entry


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
