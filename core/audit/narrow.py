"""Trail entries about a restricted case, as a non-reader reads them (#4362).

The stored entry never changes. Where rows leave for ``audit.read`` holders
(the Audit section, MCP, export, poll) and for the forwarders, an entry that
names a case the caller may not read keeps who and when, and loses the rest.
The case Record tab reads ``trail.read`` directly and keeps the full entry.
"""

from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import bindparam, select, text

from core.audit import trail

# Payload keys that name a case (the same ones ``object=`` matches, and links).
CASE_KEYS = ("case_id", "source_case_id", "related_case_id")
_KEPT_COLUMNS = (
    "seq",
    "occurred_at",
    "late",
    "entry_hash",
    "actor",
    "on_behalf_of",
    "actor_text",
)
_KEPT_PAYLOAD = (
    trail.ACTION,
    trail.ACTOR,
    trail.ON_BEHALF_OF,
    "case_id",
    trail.OUTCOME,
)


def _case_ids(rows: Iterable[dict]) -> set[str]:
    ids: set[str] = set()
    for row in rows:
        payload = row.get("payload")
        if isinstance(payload, dict):
            ids.update(
                v for k in CASE_KEYS if isinstance(v := payload.get(k), str) and v
            )
    return ids


_READER_ACTIONS = (
    "case.restrict",
    "case.readers.add",
    "case.readers.remove",
    "case.unrestrict",
)


def _gone_hidden(session, ids: set[str], reader) -> set[str]:
    """Of deleted cases ``ids``, those whose last reader list leaves ``reader`` out.

    A deleted case has no row to test, so its list is replayed from the trail.
    """
    if not ids or reader.builtin:
        return set()
    rows = session.execute(
        text(
            "SELECT t.payload FROM public.audit_trail AS t"
            " WHERE t.payload->>'action' IN :actions"
            " AND t.payload->>'case_id' IN :ids ORDER BY t.seq"
        ).bindparams(
            bindparam("actions", expanding=True), bindparam("ids", expanding=True)
        ),
        {"actions": list(_READER_ACTIONS), "ids": sorted(ids)},
    )
    readers: dict[str, Optional[set]] = {}
    for (p,) in rows:
        case_id, action = p["case_id"], p["action"]
        if action == "case.restrict":
            readers[case_id] = set(p.get("readers") or [])
        elif action == "case.unrestrict":
            readers[case_id] = None
        elif readers.get(case_id) is not None:
            if action == "case.readers.add":
                readers[case_id] |= set(p.get("added") or [])
            else:
                readers[case_id] -= set(p.get("removed") or [])
    return {
        case_id
        for case_id, names in readers.items()
        if names is not None and reader.principal_id not in names
    }


def _hidden(session, ids: set[str], reader) -> set[str]:
    """Of ``ids``, the cases ``reader`` may not read, in one query (two when
    some are deleted)."""
    if not ids:
        return set()
    from core.auth.permissions import case_read_clause
    from core.storage.models import Case

    found = session.execute(
        select(Case.case_id, case_read_clause(reader)).where(Case.case_id.in_(ids))
    ).all()
    hidden = {case_id for case_id, readable in found if not readable}
    return hidden | _gone_hidden(
        session, ids - {case_id for case_id, _ in found}, reader
    )


def _narrowed(row: dict) -> dict:
    from core.audit.words import sentence

    payload = {k: row["payload"][k] for k in _KEPT_PAYLOAD if k in row["payload"]}
    entry = {k: row[k] for k in _KEPT_COLUMNS if k in row}
    entry.update(payload=payload, sentence=sentence(payload), restricted=True)
    return entry


def entries(session, rows: list[dict], reader=None) -> list[dict]:
    """``rows`` with each entry about a case ``reader`` may not read narrowed.

    ``reader`` defaults to the caller (``current_case_reader``). The forwarders
    pass ``nobody()``: no person reads what they send.
    """
    if reader is None:
        from core.auth.permissions import current_case_reader

        reader = current_case_reader()
    hidden = _hidden(session, _case_ids(rows), reader)
    if not hidden:
        return rows
    return [_narrowed(row) if _case_ids([row]) & hidden else row for row in rows]


def entry(session, row: Optional[dict], reader=None) -> Optional[dict]:
    return entries(session, [row], reader)[0] if row is not None else None


def nobody():
    """A reader with no person bound: reads open cases only."""
    from core.auth.permissions import CaseReader

    return CaseReader()
