"""Incident reads: `GET /v1/incidents` and `GET /v1/incidents/{incident_id}`.

An `IncidentSummary` is a view, folded from the incident's records on every
request (X2: "anything here can be re-derived from the records"). There is no
stored copy of it. `rateability` is the one rule for which incidents and values
take feedback; the feedback POST refuses with it too.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from services.medic.api.decisions import (
    _CURSOR,
    _INCIDENT,
    _SCHEMA,
    Problem,
    _Bad,
    _field,
    _reject_query,
    _with_store,
    decode_cursor,
    encode_cursor,
)

_TS = re.compile(_SCHEMA["$defs"]["ts"]["pattern"])
_LIMIT = re.compile(r"[1-9][0-9]?|1[0-9][0-9]|200")
_DEFAULT_LIMIT = 50
_BOOL = {"true": True, "false": False}
_STATES = ("open", "resolved", "all")
_LANES = {"1": 1, "2": 2, "3": 3}
_SUBJECTS = ("vigil", "watcher")
_NAMES = ("state", "lane", "subject", "unrated", "include_suppressed")
_NAMES += ("opened_after", "opened_before", "cursor", "limit")
_CHILD_IDS = 20  # suppressed_children.incident_ids holds the first 20
_RATINGS = 50  # IncidentSummary.ratings maxItems
_FEEDBACK = ("agree", "disagree", "wrong_lane", "false_alarm")
# Every record that names an incident, in chain order.
_SCAN = (
    "SELECT seq, record FROM records"
    " WHERE json_extract(record, '$.body.incident_id') IS NOT NULL ORDER BY seq"
)


def rateability(summary: dict[str, Any]) -> tuple[list[str], str | None]:
    """`(allowed_feedback, why not)` for an incident's summary. Nothing is
    allowed, with X2's `not_rateable` reason, for a suppressed child, one that
    closed quietly, or one not yet routed. Watcher incidents take only agree and
    false_alarm; disagree ("the fix is wrong") needs fix text."""
    if summary["state"] == "suppressed":
        return [], "suppressed"
    if summary["resolved_how"] == "closed_quietly":
        return [], "closed_quietly"
    if summary["routed_at"] is None:
        return [], "not_routed"
    if summary["subject"] == "watcher":
        return ["agree", "false_alarm"], None
    return [v for v in _FEEDBACK if v != "disagree" or summary["has_fix"]], None


@dataclass
class _Fold:
    opened: dict[str, Any]
    last_seq: int
    last_eval: str = "true"  # it opened because its rule evaluated true
    routed_at: str | None = None
    resolving: bool = False
    resolved: dict[str, Any] | None = None  # the incident_resolved record
    suppressed_by: str | None = None
    reopen_count: int = 0
    escalation_simulated: bool = False
    ratings: dict[str, dict[str, Any]] = field(default_factory=dict)

    def apply(self, rec: dict[str, Any]) -> None:
        body, kind = rec["body"], rec["type"]
        self.last_seq = rec["seq"]
        if kind == "incident_updated":
            change = body["change"]
            if change == "routed":
                self.routed_at = self.routed_at or rec["at"]
            elif change in ("resolving", "refiring"):
                self.resolving, self.last_eval = change == "resolving", body["eval"]
            elif change == "suppressed":
                self.suppressed_by = body["suppressed_by"]
            elif change == "unsuppressed":
                self.suppressed_by = None
            elif change == "reopened":  # flapping damping: the same incident again
                self.reopen_count = body["reopen_count"]
                self.resolved, self.resolving, self.last_eval = None, False, "true"
            elif change == "escalation_simulated":
                self.escalation_simulated = True
        elif kind == "incident_resolved":
            self.resolved, self.resolving = rec, False
            self.last_eval = body.get("eval", self.last_eval)
        elif kind == "feedback" and body["source"] == "live":
            # Each admin's latest live rating; replay feedback and adjudication
            # are never ratings.
            self.ratings.pop(body["admin"], None)
            self.ratings[body["admin"]] = {
                "admin": body["admin"],
                "value": body["value"],
                **(
                    {"suggested_lane": body["suggested_lane"]}
                    if "suggested_lane" in body
                    else {}
                ),
                "at": rec["at"],
                "seq": rec["seq"],
            }

    def summary(self, children: list[str]) -> dict[str, Any]:
        o = self.opened["body"]
        rule, advice = o["rule"], o["advice"]
        if self.resolved is not None:
            state = "resolved"
        elif self.suppressed_by is not None:
            state = "suppressed"
        elif self.routed_at is None:
            state = "held"
        else:
            state = "resolving" if self.resolving else "firing"
        out = {
            "incident_id": o["incident_id"],
            "subject": o["subject"],
            "rule": (
                {k: rule[k] for k in ("id", "revision", "pack_id", "pack_version")}
                if rule
                # An unknown signature (G1 R4) has no rule; v1 never writes one.
                else {"id": None, "revision": 0, "pack_id": "unknown"}
                | {"pack_version": "unknown"}
            ),
            # No pack is held (dev rules only), so the record's own advice.
            "title": advice["summary"] if advice else rule["id"] if rule else "",
            "lane": o["lane"],
            "state": state,
            "last_eval": self.last_eval,
            "opened_at": self.opened["at"],
            "active_since": o["active_since"],
            "routed_at": self.routed_at,
            "held_until": o.get("held_by_upgrade_until"),
            "suppressed_by": self.suppressed_by,
            "suppressed_children": {
                "count": len(children),
                "incident_ids": children[:_CHILD_IDS],
            },
            "reopen_count": self.reopen_count,
            "escalation_simulated": self.escalation_simulated,
            "resolved_at": self.resolved["at"] if self.resolved else None,
            "resolved_how": self.resolved["body"]["how"] if self.resolved else None,
            "group": o.get("group", []),
            "install_shape": o["install_shape"],
            "detection_type": o["detection_type"],
            "has_fix": advice is not None,
        }
        allowed, _ = rateability(out)
        ratings = sorted(self.ratings.values(), key=lambda r: r["seq"])
        return out | {
            "rateable": bool(allowed),
            "allowed_feedback": allowed,
            "ratings": ratings[-_RATINGS:],
            "last_seq": self.last_seq,
        }


def _folds(conn: sqlite3.Connection) -> tuple[dict[str, _Fold], dict[str, list]]:
    """Every incident whose incident_opened is still held, folded in seq order,
    and the children nested under each parent.

    A suppressed child nests under the parent it names, except while it is
    still open and that parent has resolved (§6.3's wait to outlive it): then
    it is a row of its own, or no open list would show it."""
    folds: dict[str, _Fold] = {}
    for seq, raw in conn.execute(_SCAN):
        rec = json.loads(raw)
        iid = rec["body"]["incident_id"]
        if rec["type"] == "incident_opened":
            folds[iid] = _Fold(opened=rec, last_seq=seq)
            if rec["body"]["route"] == "routed":
                folds[iid].routed_at = rec["at"]
        elif iid in folds:  # its opening was purged: not an incident we can show
            folds[iid].apply(rec)
    children: dict[str, list[str]] = {}
    for iid in sorted(folds):
        child = folds[iid]
        parent = folds.get(child.suppressed_by or "")
        if child.suppressed_by is None or (
            child.resolved is None and (parent is None or parent.resolved is not None)
        ):
            continue
        children.setdefault(child.suppressed_by, []).append(iid)
    return folds, children


@dataclass(frozen=True)
class _Query:
    state: str = "open"
    lane: int | None = None
    subject: str | None = None
    unrated: bool = False
    include_suppressed: bool = False
    opened_after: str | None = None
    opened_before: str | None = None
    after: tuple[str, str] | None = None  # the cursor: last (opened_at, incident_id)
    limit: int = _DEFAULT_LIMIT

    def filters(self) -> str:
        """Short, like decisions._filters: it shares the 256-character cursor."""
        parts = [f"s={self.state}"]
        if self.lane is not None:
            parts.append(f"l={self.lane}")
        if self.subject is not None:
            parts.append(f"j={self.subject}")
        parts.append(f"u={int(self.unrated)}&x={int(self.include_suppressed)}")
        if self.opened_after is not None:
            parts.append("a=" + self.opened_after)
        if self.opened_before is not None:
            parts.append("b=" + self.opened_before)
        return "&".join(parts)


def _when(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _parse(query: str) -> _Query:
    seen: set[str] = set()
    got: dict[str, Any] = {}
    cursor = None
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name not in _NAMES:
            raise _Bad("invalid_parameter", _field(name))
        if name in seen:
            raise _Bad("invalid_parameter", name)
        seen.add(name)
        if name == "state" and value in _STATES:
            got[name] = value
        elif name == "lane" and value in _LANES:
            got[name] = _LANES[value]
        elif name == "subject" and value in _SUBJECTS:
            got[name] = value
        elif name in ("unrated", "include_suppressed") and value in _BOOL:
            got[name] = _BOOL[value]
        elif name in ("opened_after", "opened_before") and _TS.fullmatch(value):
            try:
                _when(value)
            except ValueError:
                raise _Bad("invalid_parameter", name) from None
            got[name] = value
        elif name == "limit" and _LIMIT.fullmatch(value):
            got[name] = int(value)
        elif name == "cursor":
            if _CURSOR.fullmatch(value) is None:
                raise _Bad("invalid_cursor", "cursor")
            cursor = value
        else:
            raise _Bad("invalid_parameter", name)
    q = _Query(**got)
    if cursor is None:
        return q
    decoded = decode_cursor(cursor)
    if decoded is None or decoded[0] != q.filters():
        raise _Bad("invalid_cursor", "cursor")
    at, _, iid = decoded[1].partition(" ")
    if _TS.fullmatch(at) is None or _INCIDENT.fullmatch(iid) is None:
        raise _Bad("invalid_cursor", "cursor")
    return _Query(**got, after=(at, iid))


def _matches(s: dict[str, Any], q: _Query, nested: set[str]) -> bool:
    """Every filter but `unrated` and the cursor (so `counts` ignores both)."""
    if q.state == "open" and s["state"] == "resolved":
        return False
    if q.state == "resolved" and s["state"] != "resolved":
        return False
    if q.lane is not None and s["lane"]["value"] != q.lane:
        return False
    if q.subject is not None and s["subject"] != q.subject:
        return False
    if not q.include_suppressed and s["incident_id"] in nested:
        return False  # nested under its parent instead
    opened = _when(s["opened_at"])
    if q.opened_after is not None and opened <= _when(q.opened_after):
        return False
    return q.opened_before is None or opened < _when(q.opened_before)


def _page(conn: sqlite3.Connection, q: _Query) -> dict[str, Any]:
    folds, children = _folds(conn)
    every = (f.summary(children.get(iid, [])) for iid, f in folds.items())
    nested = {iid for ids in children.values() for iid in ids}
    rows = [s for s in every if _matches(s, q, nested)]
    counts = {
        "incidents": len(rows),
        "rateable": sum(s["rateable"] for s in rows),
        "rated": sum(s["rateable"] and bool(s["ratings"]) for s in rows),
    }
    if q.unrated:
        rows = [s for s in rows if s["rateable"] and not s["ratings"]]
    # Newest opened_at first, ties by incident_id. opened_at is a ts_tick
    # (one fixed spelling), so its string order is time order.
    rows.sort(key=lambda s: s["incident_id"])
    rows.sort(key=lambda s: s["opened_at"], reverse=True)
    if q.after is not None:
        at, iid = q.after
        rows = [
            s
            for s in rows
            if s["opened_at"] < at or (s["opened_at"] == at and s["incident_id"] > iid)
        ]
    kept = rows[: q.limit]
    nxt = None
    if len(rows) > q.limit:
        last = kept[-1]
        nxt = encode_cursor(q.filters(), f"{last['opened_at']} {last['incident_id']}")
    return {"items": kept, "next_cursor": nxt, "counts": counts}


def list_incidents(data_dir: Path | None, query: str) -> dict | Problem:
    try:
        parsed = _parse(query)
    except _Bad as bad:
        return bad.problem
    return _with_store(data_dir, lambda conn: _page(conn, parsed))


def get_incident(data_dir: Path | None, incident_id: str, query: str) -> dict | Problem:
    try:
        _reject_query(query)
        if _INCIDENT.fullmatch(incident_id) is None:
            raise _Bad("invalid_parameter", "incident_id")
    except _Bad as bad:
        return bad.problem

    def read(conn: sqlite3.Connection) -> dict | Problem:
        folds, children = _folds(conn)
        fold = folds.get(incident_id)
        if fold is None:
            return Problem(404, "not_found")
        summary = fold.summary(children.get(incident_id, []))
        return {"summary": summary, "opened": fold.opened}

    return _with_store(data_dir, read)
