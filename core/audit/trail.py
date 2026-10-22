"""The audit trail's only writer (#2755 decisions 5, 6, 8 and 15).

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
from dataclasses import dataclass
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
                "action": "audit.checkpoint",
                "actor": SYSTEM,
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
