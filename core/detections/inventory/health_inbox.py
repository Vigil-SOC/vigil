"""The rule-health flags as each person's inbox, behind the console's bell.

Vigil's own moves are entries too, until the rule's stage moves again: a Revert
of a live rule (live -> shadow, check "revert") and turning a live or shadow rule
off (check "off", e.g. its new version failed lint). Their ids are negative.

An entry is one open flag (``catalog.open_flags``): a rule whose latest Runs,
Sees, Quiet, Accurate, Noisy, Silent or drift change went pass -> fail. It is
known by that change row's id, so a check that clears and breaks again comes
back unread. A flag that clears leaves the inbox.

Read state is per person: the change ids they read, in their
``user_preferences`` row under ``READ_KEY``. Each write keeps only ids still
open, so the list never outgrows the open flags.

What the bell's panel shows is kept short. Up to ``SUMMARY_AT`` unread flags
are listed one per rule; past that they fold into one summary instead: the
unread count by check and the rules they name, for one link to Rules filtered
to them (``RULES_LINKED`` at most, newest first). A read flag leaves the list;
the newest ``EARLIER`` of them are kept under "Earlier", with their count.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from sqlalchemy.dialects.postgresql import insert

from core.detections.inventory import catalog
from core.storage.connection import get_db_manager
from core.storage.models import UserPreference

READ_KEY = "rule_health_read"
SUMMARY_AT = 5
# Read flags kept under "Earlier", newest first; earlier_total counts them all.
EARLIER = 5
# Rule ids a summary links to: the Rules URL carries them.
RULES_LINKED = 200


def inbox(user_id: str) -> Dict[str, Any]:
    """The open flags, newest first, each marked read or not for ``user_id``."""
    return _inbox(_open(), _read_ids(user_id))


def mark_read(
    user_id: str, change_ids: Optional[Iterable[int]] = None
) -> Dict[str, Any]:
    """Marks ``change_ids`` read for ``user_id``, or every open flag when None;
    returns the inbox after. An id that names no open flag is ignored."""
    flags = _open()
    open_ids = {flag["change_id"] for flag in flags}
    picked = open_ids if change_ids is None else open_ids & set(change_ids)
    read = _update_read(user_id, lambda before: (before & open_ids) | picked)
    return _inbox(flags, read)


def _open() -> List[Dict[str, Any]]:
    flags = catalog.open_flags() + catalog.open_vigil_moves()
    return sorted(flags, key=lambda f: f["since"], reverse=True)


def _inbox(flags: List[Dict[str, Any]], read: Set[int]) -> Dict[str, Any]:
    entries = [{**flag, "read": flag["change_id"] in read} for flag in flags]
    unread = [entry for entry in entries if not entry["read"]]
    earlier = [entry for entry in entries if entry["read"]]
    summary = None
    if len(unread) > SUMMARY_AT:
        rule_ids = list(dict.fromkeys(entry["rule_id"] for entry in unread))
        summary = {
            "count": len(unread),
            "by_check": dict(Counter(entry["check"] for entry in unread)),
            "rules": len(rule_ids),
            "rule_ids": rule_ids[:RULES_LINKED],
            "since": unread[0]["since"],
        }
    return {
        # Unread flags one per rule, or none when the summary stands for them.
        "entries": [] if summary else unread,
        "earlier": earlier[:EARLIER],
        "earlier_total": len(earlier),
        "total": len(entries),
        "unread": len(unread),
        "summary": summary,
    }


def _ids(preferences: Any) -> Set[int]:
    raw = preferences.get(READ_KEY) if isinstance(preferences, dict) else None
    if not isinstance(raw, list):
        return set()
    return {i for i in raw if isinstance(i, int) and not isinstance(i, bool)}


def _read_ids(user_id: str) -> Set[int]:
    with get_db_manager().session_scope() as session:
        row = session.get(UserPreference, user_id)
        return _ids(row.preferences if row else {})


def _update_read(user_id: str, update: Callable[[Set[int]], Set[int]]) -> Set[int]:
    """Applies ``update`` to the stored ids under a row lock; returns the new set.
    Other preference keys are kept."""
    with get_db_manager().session_scope() as session:
        session.execute(
            insert(UserPreference)
            .values(user_id=user_id, preferences={})
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        row = session.get(UserPreference, user_id, with_for_update=True)
        preferences = dict(row.preferences or {})
        read = update(_ids(preferences))
        preferences[READ_KEY] = sorted(read)
        row.preferences = preferences
        return read
