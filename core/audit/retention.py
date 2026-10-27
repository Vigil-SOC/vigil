"""How long the trail is kept, and erasure of a principal's personal details.

The period and a pending erasure of an ``audit.manage`` holder live in
``system_config``, written on the caller's session. ``ConfigService`` is not
used: its audit helper would append a second ``config.change``. Each is
announced with one trail entry and takes effect seven days later. The ids
that have actually been blanked are one per line in
``$VIGIL_UPGRADE_DIR/erased-principals.txt``, and the same list is copied
into each backup. They are not stored in the database.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import bindparam, event, text

from core.audit import trail
from core.auth import mcp_credential_service, principals, sign_ins
from core.config import get_settings

MIN_DAYS = 365
MAX_DAYS = 36500
WAIT = timedelta(days=7)
CONFIG_KEY = "audit.retention"
CONFIG_DESCRIPTION = (
    "Audit retention period, and erasures of an audit manager waiting out seven days."
)
LIST_NAME = "erased-principals.txt"
# Queued on the session until the transaction commits. A rollback must not
# leave an id in the file: restore would blank that principal anyway.
_QUEUED = "erasure_file_ids"
_REWRITE = "erasure_file_rewrite"
_ARMED = "erasure_file_armed"

# A row names a principal when its hashed payload carries that id. The columns
# beside the hash are what erasure clears; the payload stays so the chain does.
_NAMES = """
    payload->>'actor' = :id
    OR payload->>'on_behalf_of' = :id
    OR payload->>'principal' = :id
    OR payload->>'object' = :id
    OR payload #>> '{object,id}' = :id
"""


class RetentionError(ValueError):
    """The request is refused. The router answers 400, or 404 for an unknown principal."""


def outside_path() -> Optional[Path]:
    """The erasure list. None when ``VIGIL_UPGRADE_DIR`` is unset."""
    raw = (get_settings().vigil_upgrade_dir or "").strip()
    if not raw:
        return None
    return Path(raw) / LIST_NAME


def read_ids(path: Optional[Path]) -> list[str]:
    """Principal ids in ``path``, in file order, blanks and repeats dropped."""
    if path is None or not path.is_file():
        return []
    found: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        pid = line.strip()
        if pid and pid not in seen:
            seen.add(pid)
            found.append(pid)
    return found


def list_text() -> str:
    """The outside list as the snapshot should copy it. Empty when unset."""
    return "".join(f"{pid}\n" for pid in read_ids(outside_path()))


def set_period(
    session, days: int, actor: str, now: datetime, updated_by: Optional[str] = None
) -> dict:
    """Store a period and announce it. It is not in force until ``WAIT`` has passed."""
    now = _aware(now)
    if isinstance(days, int) and not isinstance(days, bool) and days > MAX_DAYS:
        raise RetentionError(f"retention is at most {MAX_DAYS} days")
    if isinstance(days, bool) or not isinstance(days, int) or days < MIN_DAYS:
        raise RetentionError(f"retention is at least {MIN_DAYS} days")
    # The leader holds the feed row, then the trail lock, then this row.
    _lock_feed(session)
    stored = _load(session, lock=True)
    entry = trail.append(
        session,
        {trail.ACTION: trail.ACTION_RETENTION, trail.ACTOR: actor, "days": days},
    )
    stored["periods"].append(
        {"days": days, "set_at": now.isoformat(), "seq": entry.seq}
    )
    _save(session, stored, updated_by or actor)
    _remember(session, entry.seq)
    return _view(session, now)


def erase(
    session,
    actor: str,
    principal_id: str,
    reason: str,
    now: datetime,
    updated_by: Optional[str] = None,
) -> dict:
    """Blank ``principal_id``, or announce it when that principal can manage the trail.

    Refuses, before any blanking, when the upgrade directory is unset, when
    ``actor`` is erasing their own principal, or when the target is the last
    active principal holding ``audit.manage``.
    """
    now = _aware(now)
    principal_id = (principal_id or "").strip()
    reason = (reason or "").strip()
    if not principal_id:
        raise RetentionError("a principal is required")
    if outside_path() is None:
        raise RetentionError("VIGIL_UPGRADE_DIR is unset")
    if not _exists(session, principal_id):
        raise RetentionError("unknown principal")
    # Holding the permission is the role grant on a principal that still exists.
    # "Active" is only the last-manager guard: a disabled account still holds it.
    holders = _managers(session, active_only=False)
    active = _managers(session, active_only=True)
    if principal_id in active and len(active) == 1:
        raise RetentionError("the last audit manager cannot be erased")
    if actor == principal_id:
        raise RetentionError("you cannot erase your own principal")
    if principal_id in holders:
        if not reason:
            raise RetentionError("erasing an audit manager needs a reason")
        return _defer(session, actor, principal_id, reason, now, updated_by)
    _perform(session, principal_id)
    # Immediate. The trail entry is the record of the action, not a seven-day
    # announcement, so it is not tracked as unconfirmed.
    _announce(session, actor, principal_id, reason)
    return _view(session, now)


def view(session, now: datetime) -> dict:
    """What the Audit section shows. The unconfirmed flag is feed state, not a trail row."""
    return _view(session, _aware(now))


def apply_due(session, now: datetime) -> None:
    """Prefix-delete once a period is in force, and blank erasures whose wait has elapsed.

    The cut is the seq before the oldest entry still inside the window. A late
    row has an old time and a high seq; deleting through that seq would remove
    newer entries, so a prefix is removed only when every seq in it is outside
    the window. When nothing is inside the window, the tip is that prefix.
    No call is made when that seq is not a row.
    """
    now = _aware(now)
    _apply_period(session, now)
    _apply_erasures(session, now)


def carried(session, seqs: list[int]) -> None:
    """A poll or an export returned these rows, so their announcements have left."""
    if not seqs:
        return
    drop = {int(seq) for seq in seqs}
    _write_announcements(
        session, [seq for seq in _announcements(session) if seq not in drop]
    )


def carry_export(session, query, actor: str):
    """Export, after locking the feed row.

    ``trail.export`` appends, and the insert takes the trail lock. The leader
    holds the feed row and then that lock. Locking the feed row first is that
    order; taking the trail lock and then this row deadlocks with the tick.
    """
    _lock_feed(session)
    rows, tip = trail.export(session, query, actor)
    carried(session, [row["seq"] for row in rows if "seq" in row])
    return rows, tip


def lock_due_accounts(session, now: datetime) -> None:
    """Lock accounts a due erasure will change, before the trail lock.

    The leader calls this before it drains late rows. Account deletion locks
    the principal, then the user, then appends. Blanking those rows after the
    trail lock is already held deadlocks with that.
    """
    now = _aware(now)
    if outside_path() is None:
        return
    stored = _load(session, lock=True)
    ids = sorted(
        {
            item["principal_id"]
            for item in stored["erasures"]
            if _stamp(item) + WAIT <= now
        }
    )
    if not ids:
        return
    session.execute(
        text(
            "SELECT principal_id FROM public.principals"
            " WHERE principal_id IN :ids ORDER BY principal_id FOR UPDATE"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    )
    session.execute(
        text(
            "SELECT user_id FROM public.users"
            " WHERE principal_id IN :ids ORDER BY user_id FOR UPDATE"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    )


def cover_through(session, seq: int) -> None:
    """A push delivered every entry through ``seq``."""
    _write_announcements(
        session, [item for item in _announcements(session) if item > seq]
    )


def reapply(session, snapshot_ids: list[str]) -> None:
    """Blank the union of ``snapshot_ids`` and the outside file, then write that union back.

    Used after a restore, once the restored database is the live one. An id that
    exists only in the outside file is blanked again, because the snapshot was
    taken before that erasure.
    """
    union: list[str] = []
    seen: set[str] = set()
    for pid in [*snapshot_ids, *read_ids(outside_path())]:
        pid = pid.strip()
        if pid and pid not in seen:
            seen.add(pid)
            union.append(pid)
    if not union:
        return
    path = outside_path()
    if path is None:
        raise RetentionError("VIGIL_UPGRADE_DIR is unset")
    for pid in union:
        _blank_label(session, pid)
        _revoke(session, pid)
    trail._lock(session)
    for pid in union:
        _blank_beside(session, pid)
    _queue_rewrite(session, "".join(f"{pid}\n" for pid in union))


def _apply_period(session, now: datetime) -> None:
    period = _effective(_load(session, lock=True)["periods"], now)
    if period is None or not _table(session, "audit_trail"):
        return
    cutoff = now - timedelta(days=int(period["days"]))
    inside = session.execute(
        text("SELECT min(seq) FROM public.audit_trail WHERE occurred_at >= :cutoff"),
        {"cutoff": cutoff},
    ).scalar()
    if inside is None:
        # Every remaining row is outside the window. The tip is a real seq.
        through = session.execute(
            text("SELECT max(seq) FROM public.audit_trail")
        ).scalar()
        if through is None:
            return
    else:
        through = int(inside) - 1
        if (
            session.execute(
                text("SELECT 1 FROM public.audit_trail WHERE seq = :seq"),
                {"seq": through},
            ).first()
            is None
        ):
            return
    session.execute(
        text("SELECT public.audit_trail_delete_prefix(:seq)"),
        {"seq": through},
    )


def _apply_erasures(session, now: datetime) -> None:
    if outside_path() is None:
        return
    stored = _load(session, lock=True)
    due = [item for item in stored["erasures"] if _stamp(item) + WAIT <= now]
    if not due:
        return
    # The request-time guard is not enough: the others may have been disabled
    # during the wait. Blanking the one who is now last is still refused.
    active = _managers(session, active_only=True)
    gone = set()
    for item in due:
        principal_id = item["principal_id"]
        if principal_id in active and len(active) == 1:
            continue
        _perform(session, principal_id)
        active.discard(principal_id)
        gone.add((principal_id, item["set_at"]))
    stored["erasures"] = [
        item
        for item in stored["erasures"]
        if (item["principal_id"], item["set_at"]) not in gone
    ]
    _save(session, stored, None)


def _defer(session, actor, principal_id, reason, now, updated_by) -> dict:
    # Same order as a period save: the feed row, then this row, then the trail.
    _lock_feed(session)
    stored = _load(session, lock=True)
    if any(item["principal_id"] == principal_id for item in stored["erasures"]):
        return _view(session, now)
    entry = _announce(session, actor, principal_id, reason)
    stored["erasures"].append(
        {
            "principal_id": principal_id,
            "reason": reason,
            "set_at": now.isoformat(),
            "seq": entry.seq,
        }
    )
    _save(session, stored, updated_by or actor)
    _remember(session, entry.seq)
    return _view(session, now)


def _announce(session, actor: str, principal_id: str, reason: str):
    payload: dict[str, Any] = {
        trail.ACTION: trail.ACTION_ERASURE,
        trail.ACTOR: actor,
        "principal": principal_id,
    }
    if reason:
        payload["reason"] = reason
    return trail.append(session, payload)


def _perform(session, principal_id: str) -> None:
    # The label and the account come first on an immediate erasure. The
    # beside-columns wait until this transaction holds the trail lock: the
    # leader takes that lock and then locks window rows, so updating those
    # rows first deadlocks with the tick.
    _blank_label(session, principal_id)
    _revoke(session, principal_id)
    trail._lock(session)
    _blank_beside(session, principal_id)
    _queue_append(session, [principal_id])


def _blank_label(session, principal_id: str) -> None:
    if _table(session, "principals"):
        session.execute(
            text("UPDATE public.principals SET label = NULL WHERE principal_id = :id"),
            {"id": principal_id},
        )


def _blank_beside(session, principal_id: str) -> None:
    if _table(session, "audit_trail"):
        session.execute(
            text(f"""
                UPDATE public.audit_trail SET
                    source_ip = NULL, actor_text = NULL, operator_text = NULL,
                    attempted_name = NULL, personal = NULL
                WHERE {_NAMES}
                """),
            {"id": principal_id},
        )
    if _table(session, "audit_trail_held"):
        session.execute(
            text(
                f"UPDATE public.audit_trail_held SET beside = '{{}}'::jsonb WHERE {_NAMES}"
            ),
            {"id": principal_id},
        )


def _revoke(session, principal_id: str) -> None:
    """Revoke MCP credentials and end sign-ins, and keep those rows.

    The account is deactivated so it cannot sign in again or count as an
    active audit manager. Its email, name, and password stay. The principal
    is not marked deleted.
    """
    if not _table(session, "users"):
        return
    user_id = session.execute(
        text("SELECT user_id FROM public.users WHERE principal_id = :id"),
        {"id": principal_id},
    ).scalar()
    if not user_id:
        return
    for credential in mcp_credential_service.list_for_user(user_id, session=session):
        mcp_credential_service.revoke(credential.credential_id, session=session)
    sign_ins.end_all_for_user(user_id, sign_ins.REVOKED, session=session)
    # A blanked account must not sign in again, and must not count as an active holder.
    session.execute(
        text("UPDATE public.users SET is_active = false WHERE user_id = :id"),
        {"id": user_id},
    )


def _queue_append(session, ids: list[str]) -> None:
    session.info.setdefault(_QUEUED, []).extend(ids)
    _arm_file(session)


def _queue_rewrite(session, body: str) -> None:
    session.info[_REWRITE] = body
    _arm_file(session)


def _arm_file(session) -> None:
    """Write the erasure file only after this transaction commits."""
    if session.info.get(_ARMED):
        return
    session.info[_ARMED] = True

    def _committed(sess) -> None:
        event.remove(sess, "after_rollback", _rolled_back)
        rewrite = sess.info.pop(_REWRITE, None)
        append = list(sess.info.pop(_QUEUED, []))
        sess.info.pop(_ARMED, None)
        if rewrite is not None:
            _write_exact(rewrite)
        elif append:
            _append_file(append)

    def _rolled_back(sess) -> None:
        event.remove(sess, "after_commit", _committed)
        sess.info.pop(_REWRITE, None)
        sess.info.pop(_QUEUED, None)
        sess.info.pop(_ARMED, None)

    event.listen(session, "after_commit", _committed, once=True)
    event.listen(session, "after_rollback", _rolled_back, once=True)


def _write_exact(body: str) -> None:
    path = outside_path()
    if path is None:
        raise RetentionError("VIGIL_UPGRADE_DIR is unset")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _append_file(ids: list[str]) -> None:
    path = outside_path()
    if path is None:
        raise RetentionError("VIGIL_UPGRADE_DIR is unset")
    existing = set(read_ids(path))
    fresh = [pid for pid in ids if pid not in existing]
    if not fresh:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for pid in fresh:
            handle.write(pid + "\n")


def _exists(session, principal_id: str) -> bool:
    if not _table(session, "principals"):
        return False
    return (
        session.execute(
            text("SELECT 1 FROM public.principals WHERE principal_id = :id"),
            {"id": principal_id},
        ).first()
        is not None
    )


def _managers(session, *, active_only: bool) -> set[str]:
    """Principals whose role holds ``audit.manage``.

    ``active_only`` is the last-manager guard. A disabled account still holds
    the permission, so erasing it waits seven days like any other holder. A
    service account can hold the role but never exercises it (``_PERSON_ONLY``
    in ``auth_service``), so it does not count as a manager either.
    """
    if not _table(session, "users"):
        return set()
    active = "AND u.is_active" if active_only else ""
    rows = session.execute(
        text(f"""
            SELECT u.principal_id
            FROM public.users u
            JOIN public.principals p ON p.principal_id = u.principal_id
            JOIN public.roles r ON r.role_id = u.role_id
            WHERE p.deleted_at IS NULL
              AND p.kind = :person
              {active}
              AND (r.permissions -> 'audit.manage') = 'true'::jsonb
            """),
        {"person": principals.PERSON},
    ).scalars()
    return set(rows)


def _load(session, *, lock: bool = False) -> dict[str, list]:
    if not _table(session, "system_config"):
        return {"periods": [], "erasures": []}
    # FOR UPDATE locks only a row that is already there. Two first writers
    # would each read nothing, then each replace the whole document. Insert
    # the empty document first so the second writer waits and then reads the
    # first writer's commit.
    if lock:
        session.execute(
            text("""
                INSERT INTO public.system_config
                    (key, value, description, config_type, updated_by)
                VALUES (:key, CAST(:value AS jsonb), :description, 'audit', NULL)
                ON CONFLICT (key) DO NOTHING
                """),
            {
                "key": CONFIG_KEY,
                "value": json.dumps({"periods": [], "erasures": []}),
                "description": CONFIG_DESCRIPTION,
            },
        )
    value = session.execute(
        text(
            "SELECT value FROM public.system_config WHERE key = :key"
            + (" FOR UPDATE" if lock else "")
        ),
        {"key": CONFIG_KEY},
    ).scalar()
    if not isinstance(value, dict):
        return {"periods": [], "erasures": []}
    return {
        "periods": [
            item for item in value.get("periods") or [] if isinstance(item, dict)
        ],
        "erasures": [
            item for item in value.get("erasures") or [] if isinstance(item, dict)
        ],
    }


def _save(session, stored: dict, updated_by: Optional[str]) -> None:
    session.execute(
        text("""
            INSERT INTO public.system_config
                (key, value, description, config_type, updated_by)
            VALUES (:key, CAST(:value AS jsonb), :description, 'audit', :user)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """),
        {
            "key": CONFIG_KEY,
            "value": json.dumps(stored),
            "description": CONFIG_DESCRIPTION,
            "user": updated_by,
        },
    )


def _view(session, now: datetime) -> dict:
    stored = _load(session)
    periods = stored["periods"]
    erasures = stored["erasures"]
    pending = _pending(periods, now)
    effective = _effective(periods, now)
    announced = set(_announcements(session))
    # Period rows stay in the setting, so their seqs are known. Anything else
    # in the array is an audit.manage erasure announcement, including one whose
    # request was already applied: it stays flagged until a push, poll, or
    # export actually carries the row.
    period_seqs = {int(item["seq"]) for item in periods if item.get("seq") is not None}

    def waiting(item: dict) -> bool:
        seq = item.get("seq")
        return seq is not None and int(seq) in announced

    return {
        "effective_days": int(effective["days"]) if effective else None,
        "pending_days": int(pending["days"]) if pending else None,
        "pending_on": (_stamp(pending) + WAIT).isoformat() if pending else None,
        "unconfirmed": any(waiting(item) for item in periods),
        "erasure_unconfirmed": any(seq not in period_seqs for seq in announced),
        "erasures": [
            {
                "principal_id": item.get("principal_id"),
                "on": (_stamp(item) + WAIT).isoformat(),
                "unconfirmed": waiting(item),
            }
            for item in erasures
        ],
    }


def _effective(periods: list[dict], now: datetime) -> Optional[dict]:
    ready = [item for item in periods if _stamp(item) + WAIT <= now]
    if not ready:
        return None
    return max(ready, key=_stamp)


def _pending(periods: list[dict], now: datetime) -> Optional[dict]:
    waiting = [item for item in periods if _stamp(item) + WAIT > now]
    if not waiting:
        return None
    return max(waiting, key=_stamp)


def _remember(session, seq: int) -> None:
    current = _announcements(session)
    if seq not in current:
        current.append(seq)
        _write_announcements(session, current)


def _lock_feed(session) -> None:
    """The feed row, before the retention row and before the trail lock.

    ``maintenance_and_send`` holds this row, then the trail lock, then the
    retention row. A save that takes the retention row first and appends
    (the insert takes the trail lock) deadlocks with that tick.
    """
    _announcements(session)


def _announcements(session) -> list[int]:
    if not _table(session, "audit_feed_state"):
        return []
    row = session.execute(
        text(
            "SELECT pending_announcements FROM public.audit_feed_state"
            " WHERE id = 1 FOR UPDATE"
        )
    ).scalar()
    return [int(seq) for seq in (row or [])]


def _write_announcements(session, seqs: list[int]) -> None:
    if not _table(session, "audit_feed_state"):
        return
    body = "{" + ",".join(str(int(seq)) for seq in seqs) + "}"
    session.execute(
        text("""
            UPDATE public.audit_feed_state
            SET pending_announcements = CAST(:seqs AS bigint[])
            WHERE id = 1
            """),
        {"seqs": body},
    )


def _table(session, name: str) -> bool:
    return (
        session.execute(
            text("SELECT to_regclass(:name)"),
            {"name": f"public.{name}"},
        ).scalar()
        is not None
    )


def _stamp(item: dict) -> datetime:
    return _parse(item["set_at"])


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)
