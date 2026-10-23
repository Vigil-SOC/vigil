"""The audit trail's writer and the one filter its readers share (#2755).

The table, trigger and grants are revision ``0002_audit_trail``. Writers never
supply seq or hash: the trigger assigns both under the trail's transaction
advisory lock, so an append belongs to the caller's transaction and the lock
is held until that transaction ends. The functions here take a SQLAlchemy
``Session`` or ``Connection`` and never commit, except ``append_or_hold``,
which runs its own. Call them under READ COMMITTED (the default): a
REPEATABLE READ snapshot predates the lock, so its append fails on a duplicate
seq rather than chaining.

Callers check ``trail_ready()`` first: a database the upgrade has not reached
keeps today's write path.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from alembic.util import CommandError
from sqlalchemy import text

logger = logging.getLogger(__name__)

REVISION = "0002_audit_trail"
# Decision 8: one window for failed sign-ins and for refusals inside a run.
WINDOW = timedelta(seconds=60)
CHECKPOINT_EVERY = 1000
ATTEMPTED_NAME_MAX = 200
# Columns beside the hashed payload: erasure blanks them, the chain still verifies.
BESIDE = ("source_ip", "actor_text", "operator_text", "attempted_name", "personal")
SYSTEM = "system"
# Fields the hash adds to the payload; a payload may not carry them itself.
RESERVED = frozenset({"seq", "occurred_at", "late"})
# Payload keys the filter reads. Writers (#2760–#2764) use these names.
ACTOR = "actor"
ON_BEHALF_OF = "on_behalf_of"
ACTION = "action"
OBJECT = "object"
OUTCOME = "outcome"
ATTRIBUTION = "attribution"
# A result is a second entry. It names the intent's seq; it does not update it.
INTENT = "intent"
# The intent sets this true. "outcome unknown" is one no result names.
AWAITING = "awaiting"
ACTION_CHECKPOINT = "audit.checkpoint"
ACTION_EXPORT = "audit.export"
# #2768 writes the restore entry under this action. Poll's reset returns the latest.
ACTION_RESTORE = "audit.restore"
OUTCOME_DONE = "done"
OUTCOME_DENIED = "denied"
OUTCOME_RECORDED_LATE = "recorded late"
OUTCOME_UNKNOWN = "outcome unknown"
# A restore whose settle step failed after the swap (#2768).
OUTCOME_FAILED = "failed"
ATTRIBUTION_VERIFIED = "verified"
ATTRIBUTION_MATCHED_BY_NAME = "matched_by_name"
ATTRIBUTION_UNVERIFIED_LEGACY = "unverified_legacy"
# A live entry with no attribution field counts as verified.
POLL_LIMIT = 500
_READ_PAGE = 500
_VERIFY_BATCH = 5000
# The trigger's lock, and the one prefix delete takes and verify holds shared.
_LOCK = 0x7669_6769_6C41_5544  # "vigilAUD"
_PRUNE_LOCK = 0x7669_6769_6C50_524E  # "vigilPRN"

_INSERT = text("""
    INSERT INTO public.audit_trail
        (occurred_at, late, payload,
         source_ip, actor_text, operator_text, attempted_name, personal)
    VALUES (coalesce(:occurred_at, now()), :late, CAST(:payload AS jsonb),
            :source_ip, :actor_text, :operator_text, :attempted_name,
            CAST(:personal AS jsonb))
    RETURNING seq, entry_hash
""")


@dataclass(frozen=True)
class Entry:
    seq: int
    entry_hash: str


@dataclass(frozen=True)
class Verification:
    first: int
    last: int
    # The first sequence that is missing or does not verify; None when all do.
    first_bad: Optional[int] = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.first_bad is None


def _check(payload: dict[str, Any], beside: dict[str, Any]) -> None:
    """What the table would refuse, raised before anything is held."""
    if not isinstance(payload, dict) or RESERVED & payload.keys():
        raise ValueError(f"a payload is an object without {sorted(RESERVED)}")
    unknown = set(beside) - set(BESIDE)
    if unknown:
        raise TypeError(f"not a beside-column: {sorted(unknown)}")


def _beside(beside: dict[str, Any]) -> dict[str, Any]:
    values = {k: beside.get(k) for k in BESIDE}
    if values["personal"] is not None:
        values["personal"] = json.dumps(values["personal"])
    return values


def _insert(session, payload, occurred_at, late, beside) -> Entry:
    _check(payload, beside)
    row = session.execute(
        _INSERT,
        {
            "payload": json.dumps(payload),
            "occurred_at": occurred_at,
            "late": late,
            **_beside(beside),
        },
    ).one()
    return Entry(row[0], row[1])


def append(
    session,
    payload: dict[str, Any],
    *,
    occurred_at: Optional[datetime] = None,
    late: bool = False,
    **beside: Any,
) -> Entry:
    """Append one entry in the caller's transaction; it commits with the action."""
    entry = _insert(session, payload, occurred_at, late, beside)
    if entry.seq % CHECKPOINT_EVERY == 0:
        # Still inside the insert lock, so this checkpoint needs no leader.
        _insert(
            session,
            {
                ACTION: ACTION_CHECKPOINT,
                ACTOR: SYSTEM,
                "checkpoint": {"seq": entry.seq, "hash": entry.entry_hash},
            },
            None,
            False,
            {},
        )
    return entry


def hold(
    session, payload: dict[str, Any], occurred_at: datetime, **beside: Any
) -> None:
    """Keep an entry the trail would not take, to be recorded late."""
    _check(payload, beside)
    session.execute(
        text("""
            INSERT INTO public.audit_trail_held (occurred_at, payload, beside)
            VALUES (:occurred_at, CAST(:payload AS jsonb), CAST(:beside AS jsonb))
        """),
        {
            "occurred_at": occurred_at,
            "payload": json.dumps(payload),
            "beside": json.dumps({k: v for k, v in beside.items() if v is not None}),
        },
    )


def append_or_hold(payload: dict[str, Any], **beside: Any) -> Optional[Entry]:
    """For a safety action, which goes ahead whether or not this records it.

    Appends in its own transaction; if that fails, holds the entry to be
    recorded late with this time; if that fails too, the application log is
    the only record. Returns the entry, or None when it was not appended.
    """
    from core.storage.connection import get_db_session

    _check(payload, beside)  # a bad entry is the caller's bug, never held
    occurred_at = datetime.now(timezone.utc)
    try:
        with get_db_session() as session, session.begin():
            return append(session, payload, occurred_at=occurred_at, **beside)
    except Exception:  # noqa: BLE001 - the action must not depend on the trail
        logger.exception("audit trail refused an entry; holding it to record late")
    try:
        with get_db_session() as session, session.begin():
            hold(session, payload, occurred_at, **beside)
    except Exception:  # noqa: BLE001
        logger.exception(
            "audit entry could not be held: %s",
            json.dumps({"occurred_at": occurred_at.isoformat(), "payload": payload}),
        )
    return None


def drain_late(session, limit: int = 100) -> int:
    """Record held entries with their original time and the late flag.

    Each in a savepoint: one the trail refuses stays held and is logged, and
    the rest are still recorded.
    """
    rows = session.execute(
        text("""
            SELECT id, occurred_at, payload, beside FROM public.audit_trail_held
            WHERE window_key IS NULL ORDER BY id LIMIT :limit
            FOR UPDATE SKIP LOCKED
        """),
        {"limit": limit},
    ).all()
    recorded = 0
    for row_id, occurred_at, payload, beside in rows:
        try:
            with session.begin_nested():
                append(session, payload, occurred_at=occurred_at, late=True, **beside)
                session.execute(
                    text("DELETE FROM public.audit_trail_held WHERE id = :id"),
                    {"id": row_id},
                )
            recorded += 1
        except Exception:  # noqa: BLE001 - the rest still go in
            logger.exception("held audit entry %s could not be recorded", row_id)
    return recorded


def failure_key(source_ip: Optional[str], attempted_name: Optional[str]) -> str:
    """Collapse key for an unauthenticated failure."""
    name = (attempted_name or "")[:ATTEMPTED_NAME_MAX]
    return json.dumps(["sign_in_failure", source_ip, name])


def refusal_key(run_id: str, reason: str) -> str:
    """Collapse key for a refusal inside one run."""
    return json.dumps(["refusal", run_id, reason])


def _lock(session) -> None:
    """The trail lock, before any window row: append takes it after, and the
    opposite order deadlocks with a transaction that appended first."""
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK})


def _now(session) -> datetime:
    return session.execute(text("SELECT now()")).scalar_one()


def _close(session, window) -> None:
    """The window's count, as a later entry; one occurrence needs none."""
    if window.count > 1:
        append(
            session,
            {
                **window.payload,
                "collapsed": {
                    "count": window.count,
                    "first_seq": window.first_seq,
                    "first_at": window.occurred_at.isoformat(),
                    "last_at": window.last_at.isoformat(),
                },
            },
            **window.beside,
        )


_WINDOW_COLUMNS = "id, occurred_at, last_at, count, first_seq, payload, beside"


def collapse(
    session,
    key: str,
    payload: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    **beside: Any,
) -> Optional[Entry]:
    """Append the first occurrence in a window at once; only count the rest.

    ``key`` is ``failure_key`` or ``refusal_key``; it is stored hashed, since
    it holds an attacker-chosen name. Returns the entry, or None when counted.
    Holds the trail lock to the end of the transaction, counted or not.
    """
    _check(payload, beside)
    _lock(session)
    now = now or _now(session)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    params = {
        "key": digest,
        "now": now,
        "payload": json.dumps(payload),
        "beside": json.dumps({k: v for k, v in beside.items() if v is not None}),
    }
    window = None
    while window is None:  # close_windows may delete it between the two
        session.execute(
            text("""
                INSERT INTO public.audit_trail_held
                    (window_key, occurred_at, last_at, count, payload, beside)
                VALUES (:key, :now, :now, 0,
                        CAST(:payload AS jsonb), CAST(:beside AS jsonb))
                ON CONFLICT (window_key) DO NOTHING
            """),
            params,
        )
        window = session.execute(
            text(
                f"SELECT {_WINDOW_COLUMNS} FROM public.audit_trail_held "
                "WHERE window_key = :key FOR UPDATE"
            ),
            params,
        ).first()

    if window.count and window.occurred_at + WINDOW > now:
        session.execute(
            text(
                "UPDATE public.audit_trail_held SET count = count + 1, last_at = :now "
                "WHERE id = :id"
            ),
            {"now": now, "id": window.id},
        )
        return None
    if window.count:
        _close(session, window)
    entry = append(session, payload, occurred_at=now, **beside)
    session.execute(
        text("""
            UPDATE public.audit_trail_held
            SET occurred_at = :now, last_at = :now, count = 1, first_seq = :seq,
                payload = CAST(:payload AS jsonb), beside = CAST(:beside AS jsonb)
            WHERE id = :id
        """),
        {**params, "seq": entry.seq, "id": window.id},
    )
    return entry


def close_windows(
    session, *, now: Optional[datetime] = None, everything: bool = False
) -> int:
    """Close windows past 60 seconds, or every open one (the leader's start)."""
    _lock(session)
    cutoff = (now or _now(session)) - WINDOW
    windows = session.execute(
        text(f"""
            SELECT {_WINDOW_COLUMNS} FROM public.audit_trail_held
            WHERE window_key IS NOT NULL AND count > 0
              AND (:everything OR occurred_at <= :cutoff)
            ORDER BY id FOR UPDATE SKIP LOCKED
        """),
        {"everything": everything, "cutoff": cutoff},
    ).all()
    for window in windows:
        _close(session, window)
        session.execute(
            text("DELETE FROM public.audit_trail_held WHERE id = :id"),
            {"id": window.id},
        )
    return len(windows)


def verify(
    session, first: Optional[int] = None, last: Optional[int] = None
) -> Verification:
    """Check seq ``first``..``last`` (default: anchor to tip) for gaps and hashes.

    Holds the prune lock shared to the end of the transaction, so a prefix
    delete cannot remove rows between its reads and look like a gap.
    """
    session.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": _PRUNE_LOCK})
    anchor_seq, anchor_hash = session.execute(
        text("SELECT seq, entry_hash FROM public.audit_trail_anchor")
    ).one()
    tip = session.execute(text("SELECT max(seq) FROM public.audit_trail")).scalar()
    first = max(first if first is not None else 0, anchor_seq + 1)
    last = min(last if last is not None else tip or anchor_seq, tip or anchor_seq)
    if first > last:
        return Verification(first, last)

    if first == anchor_seq + 1:
        expected = anchor_hash
    else:
        expected = session.execute(
            text("SELECT entry_hash FROM public.audit_trail WHERE seq = :s"),
            {"s": first - 1},
        ).scalar()
        if expected is None:
            return Verification(first, last, first - 1, "missing")

    seq = first
    while seq <= last:
        through = min(seq + _VERIFY_BATCH - 1, last)
        rows = session.execute(
            text("""
                SELECT seq, prev_hash, entry_hash,
                       public.audit_trail_hash(prev_hash, seq, occurred_at, late, payload)
                FROM public.audit_trail WHERE seq BETWEEN :a AND :b ORDER BY seq
            """),
            {"a": seq, "b": through},
        ).all()
        for found, prev_hash, entry_hash, computed in rows:
            if found != seq:
                return Verification(first, last, seq, "missing")
            if prev_hash != expected:
                return Verification(
                    first, last, seq, "prev_hash is not the previous entry's hash"
                )
            if entry_hash != computed:
                return Verification(
                    first, last, seq, "entry_hash does not match its content"
                )
            expected = entry_hash
            seq += 1
        if seq <= through:
            return Verification(first, last, seq, "missing")
    return Verification(first, last)


def trail_ready(session) -> bool:
    """Whether the database's revision includes ``0002_audit_trail`` (decision 15)."""
    from core.upgrade.command import script_directory

    if (
        session.execute(text("SELECT to_regclass('public.alembic_version')")).scalar()
        is None
    ):
        return False
    script = script_directory()
    for (version,) in session.execute(text("SELECT version_num FROM alembic_version")):
        try:
            if any(
                r.revision == REVISION
                for r in script.iterate_revisions(version, "base")
            ):
                return True
        except CommandError:  # newer than this image: the upgrade refuses it too
            continue
    return False


# --- reading -----------------------------------------------------------------


@dataclass(frozen=True)
class TrailQuery:
    """One filter. The screen, the export, the poll and the feed all pass it."""

    since: Optional[datetime] = None
    until: Optional[datetime] = None
    actor: Optional[str] = None
    person: Optional[str] = None
    kind: Optional[str] = None
    area: Optional[str] = None
    object_ref: Optional[str] = None
    outcome: Optional[str] = None
    attribution: Optional[str] = None
    after: Optional[int] = None

    def recorded(self) -> dict[str, Any]:
        """The filter an export entry stores. Empty fields are left out."""
        out: dict[str, Any] = {}
        for key, value in (
            ("since", self.since),
            ("until", self.until),
            ("actor", self.actor),
            ("person", self.person),
            ("kind", self.kind),
            ("area", self.area),
            ("object", self.object_ref),
            ("outcome", self.outcome),
            ("attribution", self.attribution),
            ("after", self.after),
        ):
            if value is None:
                continue
            out[key] = (
                _as_utc(value).isoformat() if isinstance(value, datetime) else value
            )
        return out


@dataclass(frozen=True)
class Tip:
    seq: int
    entry_hash: str

    def public(self) -> dict[str, Any]:
        return {"seq": self.seq, "hash": self.entry_hash}


@dataclass(frozen=True)
class PollPage:
    tip: Tip
    reset: bool = False
    entries: tuple = ()
    restore: Optional[dict] = None


_OUTCOMES = {
    "done": OUTCOME_DONE,
    "denied": OUTCOME_DENIED,
    "recorded late": OUTCOME_RECORDED_LATE,
    "recorded_late": OUTCOME_RECORDED_LATE,
    "outcome unknown": OUTCOME_UNKNOWN,
    "outcome_unknown": OUTCOME_UNKNOWN,
    "failed": OUTCOME_FAILED,
}
_ATTRIBUTIONS = {
    "verified": ATTRIBUTION_VERIFIED,
    "matched by name": ATTRIBUTION_MATCHED_BY_NAME,
    "matched_by_name": ATTRIBUTION_MATCHED_BY_NAME,
    "unverified legacy": ATTRIBUTION_UNVERIFIED_LEGACY,
    "unverified_legacy": ATTRIBUTION_UNVERIFIED_LEGACY,
}
_COLUMNS = """
    t.seq, t.occurred_at, t.late, t.entry_hash, t.payload,
    t.source_ip, t.actor_text, t.operator_text, t.attempted_name, t.personal
"""
# An intent no result entry names. INTENT on the result is that intent's seq.
_UNKNOWN = (
    f"t.payload->>'{AWAITING}' = 'true' AND NOT EXISTS ("
    "SELECT 1 FROM public.audit_trail AS r "
    f"WHERE r.payload->>'{INTENT}' = CAST(t.seq AS text))"
)


def _blank(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _as_utc(value: datetime) -> datetime:
    """A naive timestamp is UTC, the same way a legacy row is read."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _one(value: Optional[str], mapping: dict[str, str], name: str) -> Optional[str]:
    value = _blank(value)
    if value is None:
        return None
    found = mapping.get(value)
    if found is None:
        raise ValueError(f"unknown {name}: {value}")
    return found


def make_query(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    actor: Optional[str] = None,
    person: Optional[str] = None,
    kind: Optional[str] = None,
    area: Optional[str] = None,
    object_ref: Optional[str] = None,
    outcome: Optional[str] = None,
    attribution: Optional[str] = None,
    after: Optional[int] = None,
) -> TrailQuery:
    return TrailQuery(
        since=since,
        until=until,
        actor=_blank(actor),
        person=_blank(person),
        kind=_blank(kind),
        area=_blank(area),
        object_ref=_blank(object_ref),
        outcome=_one(outcome, _OUTCOMES, "outcome"),
        attribution=_one(attribution, _ATTRIBUTIONS, "attribution"),
        after=after,
    )


def where_clause(query: TrailQuery) -> tuple[str, dict[str, Any]]:
    """The WHERE body over ``public.audit_trail`` aliased ``t``, and its binds.

    Fragments are fixed strings. Values go in the binds.
    """
    parts = ["TRUE"]
    params: dict[str, Any] = {}
    if query.since is not None:
        parts.append("t.occurred_at >= :since")
        params["since"] = _as_utc(query.since)
    if query.until is not None:
        parts.append("t.occurred_at <= :until")
        params["until"] = _as_utc(query.until)
    if query.actor is not None:
        parts.append(f"t.payload->>'{ACTOR}' = :actor")
        params["actor"] = query.actor
    if query.kind is not None:
        parts.append(
            "EXISTS (SELECT 1 FROM public.principals AS p"
            f" WHERE p.principal_id = t.payload->>'{ACTOR}' AND p.kind = :kind)"
        )
        params["kind"] = query.kind
    if query.area is not None:
        parts.append(f"split_part(t.payload->>'{ACTION}', '.', 1) = :area")
        params["area"] = query.area
    if query.person is not None:
        # What they did, and what was done for them.
        parts.append(
            f"(t.payload->>'{ACTOR}' = :person"
            f" OR t.payload->>'{ON_BEHALF_OF}' = :person)"
        )
        params["person"] = query.person
    if query.object_ref is not None:
        parts.append(
            f"(t.payload->>'{OBJECT}' = :object_ref"
            f" OR t.payload #>> '{{{OBJECT},id}}' = :object_ref)"
        )
        params["object_ref"] = query.object_ref
    if query.outcome == OUTCOME_RECORDED_LATE:
        parts.append("t.late")
    elif query.outcome in (OUTCOME_DONE, OUTCOME_DENIED, OUTCOME_FAILED):
        parts.append(f"t.payload->>'{OUTCOME}' = :outcome")
        params["outcome"] = query.outcome
    elif query.outcome == OUTCOME_UNKNOWN:
        parts.append(_UNKNOWN)
    elif query.outcome is not None:
        raise ValueError(f"unknown outcome: {query.outcome}")
    if query.attribution == ATTRIBUTION_VERIFIED:
        # A live entry leaves the field off. Copied history sets it.
        parts.append(
            f"(t.payload->>'{ATTRIBUTION}' IS NULL"
            f" OR t.payload->>'{ATTRIBUTION}' = :attribution)"
        )
        params["attribution"] = ATTRIBUTION_VERIFIED
    elif query.attribution is not None:
        parts.append(f"t.payload->>'{ATTRIBUTION}' = :attribution")
        params["attribution"] = query.attribution
    if query.after is not None:
        parts.append("t.seq > :after")
        params["after"] = query.after
    return " AND ".join(parts), params


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _entry(row) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "occurred_at": _iso(row["occurred_at"]),
        "late": row["late"],
        "entry_hash": row["entry_hash"],
        "payload": row["payload"],
        "source_ip": row["source_ip"],
        "actor_text": row["actor_text"],
        "operator_text": row["operator_text"],
        "attempted_name": row["attempted_name"],
        "personal": row["personal"],
    }


def read(session, query: TrailQuery, limit: Optional[int] = None) -> list[dict]:
    """Entries matching ``query``, oldest sequence first."""
    where, params = where_clause(query)
    sql = (
        f"SELECT {_COLUMNS} FROM public.audit_trail AS t"
        f" WHERE {where} ORDER BY t.seq"
    )
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    return [_entry(row) for row in session.execute(text(sql), params).mappings()]


def read_all(session, query: TrailQuery) -> list[dict]:
    """Every match, in pages, so an export does not hold one unbounded result."""
    rows: list[dict] = []
    after = query.after
    while True:
        page = read(session, replace(query, after=after), _READ_PAGE)
        rows.extend(page)
        if len(page) < _READ_PAGE:
            return rows
        after = page[-1]["seq"]


def current_tip(session) -> Tip:
    """The newest entry, or the anchor when the trail is empty."""
    row = session.execute(
        text("SELECT seq, entry_hash FROM public.audit_trail ORDER BY seq DESC LIMIT 1")
    ).first()
    if row is not None:
        return Tip(row[0], row[1])
    anchor = session.execute(
        text("SELECT seq, entry_hash FROM public.audit_trail_anchor")
    ).one()
    return Tip(anchor[0], anchor[1])


def latest_restore(session) -> Optional[dict]:
    """The newest restore entry, or None when none has been written."""
    row = (
        session.execute(
            text(
                f"SELECT {_COLUMNS} FROM public.audit_trail AS t"
                f" WHERE t.payload->>'{ACTION}' = :action"
                " ORDER BY t.seq DESC LIMIT 1"
            ),
            {"action": ACTION_RESTORE},
        )
        .mappings()
        .first()
    )
    return _entry(row) if row is not None else None


def poll(session, query: TrailQuery, limit: int = POLL_LIMIT) -> PollPage:
    """Entries after ``query.after``, or a reset when that cursor is past the tip.

    An empty page means caught up, and only when ``after`` is at or before the tip.
    """
    if query.after is None:
        raise ValueError("poll needs after")
    tip = current_tip(session)
    if query.after > tip.seq:
        return PollPage(tip, reset=True, restore=latest_restore(session))
    return PollPage(tip, entries=tuple(read(session, query, min(limit, POLL_LIMIT))))


def export(session, query: TrailQuery, actor: str) -> tuple[list[dict], Tip]:
    """The filtered entries, then one export entry whose actor is ``actor``.

    The tip is read after that append, so the closing checkpoint covers it.
    """
    rows = read_all(session, query)
    append(session, {ACTION: ACTION_EXPORT, ACTOR: actor, "filter": query.recorded()})
    return rows, current_tip(session)
