"""The Integration Health record store: one ``source_health`` row per source.

Storage tier, so the config service's save path can reset a record in its own
transaction. Every writer is best-effort: a failed health write is logged and
never fails the poll, test, tool call, push or save it describes. The judge of a record is
``core.integrations.health``. Push sources call in through
``core.storage.push_health``, which throttles the counts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from core.time import utcnow

logger = logging.getLogger(__name__)

# Integration id -> federation source id, for the sources the older polling
# loops collect. This is the one copy. ``core.integrations.health`` re-exports
# both names: it already imports this module, and the storage tier cannot
# import a capability domain.
FEDERATION_INTEGRATION = {
    "elastic": "elastic-siem",
    "azure_sentinel": "azure-sentinel",
    "aws_security_hub": "aws-security-hub",
    "microsoft_defender": "microsoft-defender",
}
LEGACY_POLLED = {
    "splunk": "splunk",
    "crowdstrike": "crowdstrike",
    "opensearch": "opensearch",
    **{v: k for k, v in FEDERATION_INTEGRATION.items()},
}

DROP_WINDOW = timedelta(hours=1)
_ERROR_MAX = 2000

FIELDS = (
    "last_attempt_at",
    "last_attempt_ok",
    "last_error",
    "last_error_kind",
    "last_success_at",
    "consecutive_failures",
    "dropped_total",
    "filtered_total",
    "dropped_window_start",
    "dropped_window",
    "returned_window",
    "stored_window",
    "filtered_window",
    "last_dropped_at",
    "last_data_at",
    "labels",
    "facts",
    "last_check_at",
    "last_check_ok",
    "last_check_error",
    "reset_at",
    "updated_at",
)

Outcome = Tuple[bool, str]


def _clip(error: Optional[str]) -> Optional[str]:
    return error[:_ERROR_MAX] if error else None


def _locked(session, source_id: str):
    """The row for ``source_id``, created if missing, locked for this write."""
    from sqlalchemy.dialects.postgresql import insert

    from core.storage.models import SourceHealth

    session.execute(
        insert(SourceHealth)
        .values(source_id=source_id)
        .on_conflict_do_nothing(index_elements=["source_id"])
    )
    return (
        session.query(SourceHealth)
        .filter_by(source_id=source_id)
        .with_for_update()
        .one()
    )


def _write(source_id: str, apply) -> None:
    """Apply ``apply(row, now)`` in its own transaction; a failure is logged only."""
    try:
        from core.storage.connection import get_db_manager

        with get_db_manager().session_scope() as session:
            now = utcnow()
            row = _locked(session, source_id)
            apply(row, now)
            row.updated_at = now
    except Exception as e:  # noqa: BLE001 - health never fails what it records
        logger.warning("Health write for %s failed: %s", source_id, type(e).__name__)


def _dump(row) -> Dict[str, Any]:
    return {f: getattr(row, f) for f in FIELDS}


def read_all() -> Dict[str, Dict[str, Any]]:
    """Every record by source id; a store error raises."""
    from core.storage.connection import get_db_manager
    from core.storage.models import SourceHealth

    with get_db_manager().session_scope() as session:
        return {r.source_id: _dump(r) for r in session.query(SourceHealth).all()}


def read(source_id: str) -> Optional[Dict[str, Any]]:
    """One record, or None; a store error raises."""
    from core.storage.connection import get_db_manager
    from core.storage.models import SourceHealth

    with get_db_manager().session_scope() as session:
        row = session.get(SourceHealth, source_id)
        return _dump(row) if row else None


def _attempt(row, now, ok: bool, error: Optional[str], kind: Optional[str]) -> None:
    row.last_attempt_at = now
    row.last_attempt_ok = ok
    row.last_error = None if ok else _clip(error)
    row.last_error_kind = None if ok else kind


# Sender-controlled. The writer also caps how many a process remembers at once.
LABEL_CAP = 32
_LABEL_MAX = 128


# Federation poll counts kept under the drop window's anchor.
POLL_COUNTS = ("returned_window", "stored_window", "filtered_window")


def _restart_window(row, now) -> None:
    """A fresh window: the drop count, and poll counts already being kept."""
    row.dropped_window_start, row.dropped_window = now, 0
    for f in POLL_COUNTS:
        if getattr(row, f) is not None:
            setattr(row, f, 0)


def _counts(
    row,
    now,
    *,
    received: int,
    dropped: int,
    labels: Optional[Dict[str, str]],
) -> None:
    if received > 0:
        row.last_data_at = now
    if dropped > 0:
        start = row.dropped_window_start
        if start is None or now - start >= DROP_WINDOW:
            _restart_window(row, now)
        row.dropped_window += dropped
        row.dropped_total = (row.dropped_total or 0) + dropped
        row.last_dropped_at = now
    if not labels:
        return
    current = dict(row.labels or {})
    for label, seen in labels.items():
        text = str(label).strip()[:_LABEL_MAX]
        if not text:
            continue
        prev = current.get(text)
        if prev is None or str(seen) > str(prev):
            current[text] = seen
    if len(current) > LABEL_CAP:
        current = dict(
            sorted(current.items(), key=lambda kv: str(kv[1]), reverse=True)[:LABEL_CAP]
        )
    row.labels = current


def record_collected(
    source_id: str,
    *,
    dropped: int = 0,
    received: int = 0,
    labels: Optional[Dict[str, str]] = None,
) -> None:
    """A collection (scheduled poll, sync or accepted push) that succeeded."""

    def apply(row, now):
        _attempt(row, now, True, None, None)
        row.last_success_at = now
        row.consecutive_failures = 0
        _counts(row, now, received=received, dropped=dropped, labels=labels)

    _write(source_id, apply)


def record_polled(
    source_id: str,
    *,
    returned: int,
    stored: int,
    filtered: int,
    dropped: int,
) -> None:
    """A successful federation poll: a collection plus its window counts.

    The window rolls over before counting every batch, even an empty one, and
    the first poll after upgrade (counts still null) restarts it so earlier
    drops are not mixed in.
    """

    def apply(row, now):
        _attempt(row, now, True, None, None)
        row.last_success_at = now
        row.consecutive_failures = 0
        start = row.dropped_window_start
        if (
            start is None
            or now - start >= DROP_WINDOW
            or any(getattr(row, f) is None for f in POLL_COUNTS)
        ):
            row.dropped_window_start, row.dropped_window = now, 0
            for f in POLL_COUNTS:
                setattr(row, f, 0)
        row.returned_window += max(returned, 0)
        row.stored_window += max(stored, 0)
        row.filtered_window += max(filtered, 0)
        # Floor skips are history, like dropped_total, and survive the window roll.
        row.filtered_total = (row.filtered_total or 0) + max(filtered, 0)
        _counts(row, now, received=stored, dropped=max(dropped, 0), labels=None)

    _write(source_id, apply)


def record_dropped(
    source_id: str,
    dropped: int,
    *,
    labels: Optional[Dict[str, str]] = None,
) -> None:
    """Unreadable records that are not themselves a success or a failure.

    Does not move the last attempt, so a receiver with nothing accepted yet
    stays "requires a test".
    """
    if dropped <= 0 and not labels:
        return

    def apply(row, now):
        _counts(row, now, received=0, dropped=max(dropped, 0), labels=labels)

    _write(source_id, apply)


def record_failed(
    source_id: str,
    error: str,
    kind: str = "unknown",
    *,
    dropped: int = 0,
    received: int = 0,
    labels: Optional[Dict[str, str]] = None,
) -> None:
    """A collection that failed: one more in the run of failures."""

    def apply(row, now):
        _attempt(row, now, False, error, kind)
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        _counts(row, now, received=received, dropped=dropped, labels=labels)

    _write(source_id, apply)


def _collection_polled(source_id: str) -> bool:
    """True when scheduled collection, not a lookup, owns this source's failure run."""
    return source_id in LEGACY_POLLED


def record_lookup(source_id: str) -> None:
    """A real MCP read succeeded.

    Not a Test and not a collection, so ``last_check_*`` stays as it is.
    A source scheduled collection polls writes nothing: the read does not end
    a collection failure run, and ``updated_at`` stays put. Every other
    integration ends that run and stamps last success.
    """
    if _collection_polled(source_id):
        return

    def apply(row, now):
        _attempt(row, now, True, None, None)
        row.last_success_at = now
        row.consecutive_failures = 0

    _write(source_id, apply)


def record_facts(source_id: str, facts: Dict[str, Any], *, started: datetime) -> None:
    """A collector's successful read of the source: its facts, and a success as
    ``record_lookup`` stamps one. A failed read goes to ``record_failed`` and
    leaves the facts as they were. ``last_check_*`` is not touched. Dropped when
    the record was reset after the read ``started``: it read the old settings."""

    def apply(row, now):
        if row.reset_at is not None and row.reset_at >= started:
            return
        row.facts = facts
        _attempt(row, now, True, None, None)
        row.last_success_at = now
        row.consecutive_failures = 0

    _write(source_id, apply)


def record_recheck(
    source_id: str,
    *,
    success: bool,
    error: Optional[str],
    kind: Optional[str] = None,
) -> None:
    """A daily re-check. Both outcomes stamp ``last_check_*``.

    A source scheduled collection polls keeps the failure run on success:
    ``consecutive_failures``, ``last_attempt_*``, ``last_error*`` and
    ``last_success_at`` stay as the last collection wrote them. A failed
    re-check still adds one. Every other integration's success ends the run.
    """
    polled = _collection_polled(source_id)

    def apply(row, now):
        if not (polled and success):
            _attempt(row, now, success, error, None if success else (kind or "unknown"))
            if success:
                row.last_success_at = now
                row.consecutive_failures = 0
            else:
                row.consecutive_failures = (row.consecutive_failures or 0) + 1
        row.last_check_at = now
        row.last_check_ok = success
        row.last_check_error = None if success else _clip(error)

    _write(source_id, apply)


def stamp_recheck(session, source_id: str, now: datetime) -> None:
    """Claim today's re-check inside the caller's transaction.

    The outcome is written later by ``record_recheck``. Another replica then
    sees ``last_check_at`` and does not check the same source again today.
    """
    row = _locked(session, source_id)
    row.last_check_at = now
    row.updated_at = now


def record_test(
    source_id: str,
    *,
    success: bool,
    error: Optional[str],
    check: Optional[Outcome] = None,
    when: Optional[datetime] = None,
) -> None:
    """Test's result: the last attempt, and the last real check when one ran.

    Not a collection, so the run of collection failures is left as it is.
    """

    def apply(row, now):
        at = when.replace(tzinfo=None) if when else now
        _attempt(row, at, success, error, None)
        if check is not None:
            row.last_check_at = at
            row.last_check_ok = check[0]
            row.last_check_error = None if check[0] else _clip(check[1])

    _write(source_id, apply)


def reset(source_id: str, session=None) -> None:
    """Back to "no attempt yet": switched on, or its credential or endpoint changed.

    Cumulative drops, floor skips and the last data time stay; they are history,
    not health. Window counts are cleared.
    With ``session`` the reset joins the caller's transaction.
    """

    def apply(row, now):
        row.last_attempt_at = row.last_attempt_ok = None
        row.last_error = row.last_error_kind = None
        row.last_success_at = None
        row.consecutive_failures = 0
        row.dropped_window_start, row.dropped_window = None, 0
        for f in POLL_COUNTS:
            setattr(row, f, None)
        row.last_dropped_at = None
        row.last_check_at = row.last_check_ok = row.last_check_error = None
        row.facts = None  # another instance's, or read with another token
        row.reset_at = now
        row.updated_at = now

    if session is None:
        _write(source_id, apply)
    else:
        apply(_locked(session, source_id), utcnow())


def seed(records: Dict[str, Tuple[bool, Optional[str], Optional[str]]]) -> None:
    """Give sources with no record one from what is already known of them.

    ``{source_id: (ok, error, kind)}``; a source that has a record keeps it. For
    a status kept elsewhere that was settled before this record existed.
    """
    if not records:
        return
    try:
        from sqlalchemy.dialects.postgresql import insert

        from core.storage.connection import get_db_manager
        from core.storage.models import SourceHealth

        now = utcnow()
        rows = [
            {
                "source_id": source_id,
                "last_attempt_at": now,
                "last_attempt_ok": ok,
                "last_error": None if ok else _clip(error),
                "last_error_kind": None if ok else kind,
                "last_success_at": now if ok else None,
                "consecutive_failures": 0 if ok else 1,
                "updated_at": now,
            }
            for source_id, (ok, error, kind) in records.items()
        ]
        with get_db_manager().session_scope() as session:
            session.execute(
                insert(SourceHealth)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["source_id"])
            )
    except Exception as e:  # noqa: BLE001 - e.g. a process with no database
        logger.debug("Health seed failed: %s", type(e).__name__)
