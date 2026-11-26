"""How long the trail is kept, and erasure of a principal's personal details.

The period and a pending erasure of an ``audit.manage`` holder live in
``system_config``, written on the caller's session. ``ConfigService`` is not
used: its audit helper would append a second ``config.change``. Each is
announced with one trail entry and takes effect seven days later. The ids
that have actually been blanked are one per line in
``$VIGIL_ERASURE_DIR/erased-principals.txt`` (``$VIGIL_UPGRADE_DIR`` when that
is unset), and the same list is copied into each backup. They are not stored in
the database. Replicas share the list on one volume, so each write takes a
Postgres advisory lock and is fsynced before the lock is released.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional

from sqlalchemy import bindparam, event, text

from core.audit import trail
from core.auth import (
    identity,
    key_ring,
    mcp_credential_service,
    principals,
    sign_ins,
    trail_events,
)
from core.config import get_settings
from core.storage.connection import get_db_manager
from core.time import utcnow

MIN_DAYS = 365
MAX_DAYS = 36500
WAIT = timedelta(days=7)
CONFIG_KEY = "audit.retention"
CONFIG_DESCRIPTION = (
    "Audit retention period, and erasures of an audit manager waiting out seven days."
)
LIST_NAME = "erased-principals.txt"
# Replicas take turns writing the list. Distinct from the trail's, the feed's,
# the upgrade's and the rule sync's keys.
_LIST_LOCK = 0x7669_6769_6C45_5253  # "vigilERS"
# One refusal wherever the list has nowhere to live; restore prefixes it.
UNSET_MESSAGE = (
    "the erasure list has no directory: set VIGIL_ERASURE_DIR (or VIGIL_UPGRADE_DIR), "
    "and on Helm give stateDirectory.volume shared storage such as a ReadWriteMany claim"
)
# An erased account's username and email, derived from its principal id so the
# originals are free again. The username is how an account reads as Erased.
ERASED_PREFIX = "erased-"
ERASED_DOMAIN = "erased.invalid"
ERASED_NAME = "Erased account"
# What an erased account's token labels become: a free-text label can name a person.
ERASED_LABEL = "Erased account's token"
# Queued on the session until the transaction commits. A rollback must not
# leave an id in the file: restore would blank that principal anyway.
_QUEUED = "erasure_file_ids"
_REWRITE = "erasure_file_rewrite"
_ARMED = "erasure_file_armed"
# Open invite rows locked before the trail lock, by the leader's tick.
# Absent: this erasure has not locked them yet. An empty tuple: it looked.
_INVITE_ROWS = "erasure_invite_rows"

# A row names a principal when its hashed payload carries that id. The columns
# beside the hash are what erasure clears; the payload stays so the chain does.
# An owner or task-assignee change keeps its holders' usernames beside it.
_NAMES = """
    payload->>'actor' = :id
    OR payload->>'on_behalf_of' = :id
    OR payload->>'principal' = :id
    OR payload->>'object' = :id
    OR payload #>> '{object,id}' = :id
    OR payload->>'assignee' = :id
    OR payload->>'previous_assignee' = :id
"""
# Invite entries are written before the account exists, so they carry the link
# id, not the principal. Erasure still has to clear them.
_INVITE_ACTIONS = (
    "invite.sent",
    "invite.failed",
    "invite.expired",
    "invite.revoked",
)
_ACCEPTED_NOTICE = "{} accepted your invite and now has an account."
_CANCELLED_SUFFIX = " now has an account."


class RetentionError(ValueError):
    """The request is refused. The router answers 400, or 404 for an unknown principal."""


def erased_username(principal_id: str) -> str:
    return f"{ERASED_PREFIX}{principal_id}"


def is_erased(username: Optional[str], principal_id: Optional[str]) -> bool:
    return bool(principal_id) and username == erased_username(principal_id)


def free_identifiers(session, principal_id: str) -> None:
    """Rewrite the account's username, email and full name to opaque values, so
    nothing readable stays and the username and email can be used again. Every
    record that keeps the username as text follows (``_USERNAME_COLUMNS``), so
    neither the name stays readable nor does a new account of that name inherit
    them. Plain SQL: an upgrade step runs it on its own connection too."""
    if not _table(session, "users"):
        return
    params = {
        "id": principal_id,
        "prefix": ERASED_PREFIX,
        "domain": ERASED_DOMAIN,
        "name": ERASED_NAME,
    }
    username = session.execute(
        text("SELECT username FROM public.users WHERE principal_id = :id"),
        {"id": principal_id},
    ).scalar()
    # Vigil's own rows name it "system": a person of that name cannot be told apart.
    if username and username != trail.SYSTEM and not is_erased(username, principal_id):
        _rename_records(session, username, erased_username(principal_id))
    session.execute(
        text("""
            UPDATE public.users SET
                username = :prefix || principal_id,
                email = :prefix || principal_id || '@' || :domain,
                full_name = :name
            WHERE principal_id = :id
            """),
        params,
    )


def drop_personal_rows(session, user_id: str) -> None:
    """Delete the account's preferences row and blank the label of each token it
    owns, revoked or not: a label is free text and may name the person. Plain
    SQL: an upgrade step runs it on its own connection too."""
    params = {"user_id": user_id, "label": ERASED_LABEL}
    if _table(session, "user_preferences"):
        session.execute(
            text("DELETE FROM public.user_preferences WHERE user_id = :user_id"),
            params,
        )
    if _table(session, "mcp_credentials"):
        session.execute(
            text(
                "UPDATE public.mcp_credentials SET label = :label WHERE user_id = :user_id"
            ),
            params,
        )


def outside_path() -> Optional[Path]:
    """The erasure list: in ``VIGIL_ERASURE_DIR``, else ``VIGIL_UPGRADE_DIR``. None when both are unset."""
    settings = get_settings()
    raw = (settings.vigil_erasure_dir or settings.vigil_upgrade_dir or "").strip()
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

    Refuses, before any blanking, when the erasure list has no directory or
    can't be written, when the key that hashes the username and email can't be
    read, when ``actor`` is erasing their own principal, or when the target is the last
    active principal holding ``audit.manage``.
    """
    now = _aware(now)
    principal_id = (principal_id or "").strip()
    reason = (reason or "").strip()
    if not principal_id:
        raise RetentionError("a principal is required")
    _check_writable()
    _check_key()
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
    # The feed row first, as every other path takes it, before the trail lock.
    _lock_feed(session)
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


# How long an erased person's username and email stay reserved.
RESERVATION_TTL = timedelta(days=730)
ACTION_RESERVATIONS_DROPPED = "auth.identifier_reservations_dropped"


def sweep_reservations(session, now: datetime) -> list[int]:
    """Drop reservations older than two years, and those whose key can't be
    read, with one system entry. Returns the ids of notices to email once the
    caller commits: a lost key tells each ``users.write`` holder.

    Then retires an ``identifier`` key no remaining row names; rotation never
    does. With no reservations and no old key this reads two small queries and
    writes nothing. The entry holds a count and a reason, never an identifier.
    """
    if not _table(session, "identifier_reservations"):
        return []
    # Naive UTC, as the column is.
    cutoff = _aware(now).replace(tzinfo=None) - RESERVATION_TTL
    expired = session.execute(
        text("DELETE FROM public.identifier_reservations WHERE reserved_at < :c"),
        {"c": cutoff},
    ).rowcount
    lost = 0
    named = set(
        session.execute(
            text("SELECT DISTINCT key_version FROM public.identifier_reservations")
        )
        .scalars()
        .all()
    )
    for version in sorted(named):
        try:
            usable = key_ring.find("identifier", version) is not None
        except key_ring.KeyRingError:
            usable = False
        if not usable:
            lost += session.execute(
                text(
                    "DELETE FROM public.identifier_reservations"
                    " WHERE key_version = :v"
                ),
                {"v": version},
            ).rowcount
            named.discard(version)
    # Only an old version: erasure and identity.reserve need the current key.
    for version in session.execute(
        text(
            "SELECT version FROM public.auth_keys WHERE purpose = 'identifier'"
            " AND state IN ('verify_only', 'decrypt_only')"
        )
    ).scalars():
        if version not in named:
            key_ring._retire(session, "identifier", version, reason="unreferenced")
            key_ring.forget()
    if not (expired or lost):
        return []
    reason = (
        "expired_and_key_lost"
        if expired and lost
        else ("expired" if expired else "key_lost")
    )
    trail.append(
        session,
        {
            trail.ACTION: ACTION_RESERVATIONS_DROPPED,
            trail.ACTOR: trail.SYSTEM,
            "count": expired + lost,
            "reason": reason,
        },
        gated=False,
    )
    return _tell_key_lost(session, lost) if lost else []


def _tell_key_lost(session, lost: int) -> list[int]:
    # Imported here: auth_service pulls auth, and auth imports this module.
    from core.auth.auth_service import _manager_rows
    from core.platform.notices import notify

    message = (
        f"{lost} reserved sign-in name{'s' if lost != 1 else ''} could not be "
        "kept, because the key that protected them is gone. Someone else may now "
        "register a username or email an erased person used."
    )
    ids = []
    for principal, _, _ in _manager_rows("", session):
        notice = notify(
            principal,
            "identifier_reservations_lost",
            "Reserved sign-in names were dropped",
            message,
            link="/settings?section=users",
            priority="high",
            session=session,
        )
        if notice is not None:
            ids.append(notice)
    return ids


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
    """Lock the retention row, then accounts a due erasure will change.

    The leader calls this before it drains late rows. A period save holds the
    retention row and then appends, and account deletion locks the principal
    and the user before it appends. Taking the trail lock first deadlocks
    with either.
    """
    now = _aware(now)
    stored = _load(session, lock=True)
    if _blocked() is not None:
        return
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
    # Invites too, before the tick takes the trail lock. Issuing one locks the
    # open row and then appends; locking the row afterwards deadlocks with it.
    traces = [_invite_trace(session, principal_id) for principal_id in ids]
    session.info[_INVITE_ROWS] = _open_invites(session, traces, lock=True)


def cover_through(session, seq: int) -> None:
    """A push delivered every entry through ``seq``."""
    _write_announcements(
        session, [item for item in _announcements(session) if item > seq]
    )


def reapply(session, snapshot_ids: list[str], *, reserve: bool = True) -> None:
    """Blank the union of ``snapshot_ids`` and the outside file, then write that union back.

    Used after a restore, once the restored database is live and its key ring
    is settled, so each person's username and email are reserved. An id that
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
        raise RetentionError(UNSET_MESSAGE)
    # A backup from before the table existed is reserved by no one: its upgrade
    # creates the table empty, and the names can't be hashed afterwards.
    reserve = reserve and _table(session, "identifier_reservations")
    # Before anything is blanked: an unreadable ring would roll the whole
    # re-apply back at the first reservation, so refuse with nothing touched.
    try:
        if reserve:
            key_ring.current("identifier")
    except key_ring.KeyRingError as exc:
        raise RetentionError(f"the identifier key can't be read: {exc}") from exc
    for pid in union:
        _blank_label(session, pid)
    # Before _revoke rewrites the addresses the scrub has to read. One pass,
    # so every open invite is locked before the first trail append.
    scrub_invites(session, union, notices=True)
    for pid in union:
        # Dated now, the re-apply time. _revoke skips service and erased accounts.
        _revoke_and_drop(session, pid, reserve=reserve)
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
    # An id that can't reach the list would be undone by a restore, and a
    # username that can't be hashed is lost, so a due erasure stays pending until
    # the list is writable and the identifier key reads.
    if _blocked() is not None:
        return
    stored = _load(session, lock=True)
    due = [item for item in stored["erasures"] if _stamp(item) + WAIT <= now]
    if not due:
        return
    # The request-time guard is not enough: the others may have been disabled
    # during the wait. Blanking the one who is now last is still refused.
    active = _managers(session, active_only=True)
    # Nor may it take the last active role manager: the request checked that,
    # and the wait may have changed it.
    roles = _managers(session, active_only=True, permission="roles.manage")
    gone = set()
    for item in due:
        principal_id = item["principal_id"]
        if principal_id in active and len(active) == 1:
            continue
        if roles == {principal_id}:
            continue
        _perform(session, principal_id)
        active.discard(principal_id)
        roles.discard(principal_id)
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


class _InviteTrace(NamedTuple):
    emails: tuple[str, ...]
    folded: tuple[str, ...]
    usernames: tuple[str, ...]
    link_ids: tuple[str, ...]


def _invite_trace(session, principal_id: str) -> _InviteTrace:
    """Addresses and usernames still stored for this person, and invite links
    whose row names them. Read before the account is rewritten and before the
    beside-columns are cleared. The rewritten ``erased-…`` address is skipped."""
    emails: set[str] = set()
    usernames: set[str] = set()
    skipped = identity.fold(f"{ERASED_PREFIX}{principal_id}@{ERASED_DOMAIN}")
    erased_user = erased_username(principal_id)

    def add_email(value: Optional[str]) -> None:
        if value and identity.fold(value) != skipped:
            emails.add(value)

    def add_username(value: Optional[str]) -> None:
        if value and value != erased_user:
            usernames.add(value)

    if _table(session, "users"):
        row = session.execute(
            text("SELECT username, email FROM public.users WHERE principal_id = :id"),
            {"id": principal_id},
        ).first()
        if row is not None:
            add_username(row.username)
            add_email(row.email)
    link_ids: list[str] = []
    if _table(session, "auth_links"):
        link_ids = list(
            session.execute(
                text(
                    "SELECT link_id FROM public.auth_links"
                    " WHERE principal_id = :id AND kind = 'invite'"
                ),
                {"id": principal_id},
            ).scalars()
        )
    _collect_personal(
        session,
        "audit_trail",
        "personal",
        principal_id,
        link_ids,
        add_email,
        add_username,
    )
    _collect_personal(
        session,
        "audit_trail_held",
        "beside->'personal'",
        principal_id,
        link_ids,
        add_email,
        add_username,
    )
    folded = tuple(sorted({identity.fold(email) for email in emails}))
    return _InviteTrace(
        tuple(sorted(emails)), folded, tuple(sorted(usernames)), tuple(link_ids)
    )


def _collect_personal(
    session, table, personal, principal_id, link_ids, add_email, add_username
) -> None:
    """Emails and usernames from ``personal`` on rows that name the principal,
    plus emails still on the invite entries of the person's links."""
    if not _table(session, table):
        return
    # The principal, not the actor. An invite they sent, and an account they
    # created, store the other person's address beside their own actor id.
    for email, username in session.execute(
        text(
            f"SELECT {personal}->>'email', {personal}->>'username'"
            f" FROM public.{table} WHERE payload->>'principal' = :id"
        ),
        {"id": principal_id},
    ):
        add_email(email)
        add_username(username)
    if not link_ids:
        return
    for email in session.execute(
        text(
            f"SELECT {personal}->>'email' FROM public.{table}"
            " WHERE payload->>'action' = ANY(CAST(:actions AS text[]))"
            " AND payload->>'link_id' = ANY(CAST(:links AS text[]))"
        ),
        {"actions": list(_INVITE_ACTIONS), "links": list(link_ids)},
    ).scalars():
        add_email(email)


def _invite_match(
    email_sql: str, link_ids: tuple[str, ...], folded: tuple[str, ...]
) -> tuple[str, dict]:
    """WHERE fragment for invite entries of these links or these addresses.
    Address comparison is ``sql_fold``: ``lower(normalize(..., NFKC))``."""
    parts: list[str] = []
    params: dict[str, Any] = {"actions": list(_INVITE_ACTIONS)}
    if link_ids:
        parts.append("payload->>'link_id' = ANY(CAST(:links AS text[]))")
        params["links"] = list(link_ids)
    if folded:
        parts.append(
            f"lower(normalize({email_sql}, NFKC)) = ANY(CAST(:folded AS text[]))"
        )
        params["folded"] = list(folded)
    if not parts:
        return "", params
    clause = (
        "payload->>'action' = ANY(CAST(:actions AS text[])) AND ("
        + " OR ".join(parts)
        + ")"
    )
    return clause, params


def _drop_invite_notices(session, trace: _InviteTrace) -> None:
    """Delete the inviter's invite notices that name this person. Other kinds stay."""
    if not _table(session, "case_notifications"):
        return
    parts: list[str] = []
    params: dict[str, Any] = {}
    accepted = [_ACCEPTED_NOTICE.format(name) for name in trace.usernames]
    if accepted:
        parts.append(
            "notification_type = 'invite_accepted'"
            " AND message = ANY(CAST(:accepted AS text[]))"
        )
        params["accepted"] = accepted
    if trace.emails:
        parts.append(
            "notification_type = 'invite_cancelled'"
            " AND message = ANY(CAST(:cancelled AS text[]))"
        )
        params["cancelled"] = [f"{email}{_CANCELLED_SUFFIX}" for email in trace.emails]
    if trace.folded:
        # The same address in another spelling: the message is the address
        # plus this suffix, compared the way identifiers compare.
        parts.append(
            "notification_type = 'invite_cancelled'"
            " AND length(message) > length(:suffix)"
            " AND right(message, length(:suffix)) = :suffix"
            " AND lower(normalize(left(message, length(message) - length(:suffix)),"
            " NFKC)) = ANY(CAST(:folded AS text[]))"
        )
        params["suffix"] = _CANCELLED_SUFFIX
        params["folded"] = list(trace.folded)
    if not parts:
        return
    session.execute(
        text(f"DELETE FROM public.case_notifications WHERE {' OR '.join(parts)}"),
        params,
    )


def _open_invites(session, traces: list[_InviteTrace], *, lock: bool):
    """Open invites for these addresses, expired ones included.

    ``lock`` takes them in link-id order before any trail append. Issuing an
    invite locks the open row and then appends, and the other order deadlocks.
    Times are naive UTC, the same clock ``note_expired`` and ``revoke`` use.
    """
    folded = tuple(sorted({email for trace in traces for email in trace.folded}))
    if not folded or not _table(session, "auth_links"):
        return ()
    locking = " FOR UPDATE" if lock else ""
    return session.execute(
        text(f"""
            SELECT link_id, email, role_id,
                   (expired_noted_at IS NULL AND expires_at <= :now) AS due
            FROM public.auth_links
            WHERE kind = 'invite' AND used_at IS NULL AND revoked_at IS NULL
              AND email IS NOT NULL
              AND lower(normalize(email, NFKC)) = ANY(CAST(:folded AS text[]))
            ORDER BY link_id{locking}
            """),
        {"folded": list(folded), "now": utcnow()},
    ).all()


def _lock_and_close(session, traces: list[_InviteTrace]) -> tuple[str, ...]:
    """Revoke open invites for these addresses, expired ones included.

    The leader's tick locks the rows before it takes the trail lock and leaves
    them on the session; locking again here would wait on an invite that is
    itself waiting for that lock. The new entries still carry the address, as
    ``note_expired`` and ``revoke`` write it; the caller blanks them once it
    holds the trail lock. Returns the closed link ids.
    """
    info = getattr(session, "info", None)
    if info is not None and _INVITE_ROWS in info:
        rows = info[_INVITE_ROWS]
    else:
        rows = _open_invites(session, traces, lock=True)
    closed: list[str] = []
    now = utcnow()
    for row in rows:
        if row.due:
            marked = session.execute(
                text("""
                    UPDATE public.auth_links SET expired_noted_at = :now
                    WHERE link_id = :id AND expired_noted_at IS NULL
                    RETURNING link_id
                    """),
                {"id": row.link_id, "now": now},
            ).first()
            if marked:
                trail_events.record(
                    session,
                    "invite.expired",
                    {"personal": {"email": row.email}},
                    actor=trail.SYSTEM,
                    link_id=row.link_id,
                    role_id=row.role_id,
                )
        updated = session.execute(
            text("""
                UPDATE public.auth_links
                SET revoked_at = :now, email = NULL
                WHERE link_id = :id AND revoked_at IS NULL
                RETURNING link_id
                """),
            {"id": row.link_id, "now": now},
        ).first()
        if updated:
            trail_events.record(
                session,
                "invite.revoked",
                {"personal": {"email": row.email}},
                actor=trail.SYSTEM,
                link_id=row.link_id,
                reason="erased",
            )
            closed.append(row.link_id)
    return tuple(closed)


def _blank_invite_rows(session, trace: _InviteTrace) -> None:
    """Null the address and source address on the invite entries of these links
    and addresses. The payload is untouched, so the chain still verifies."""
    clause, params = _invite_match("personal->>'email'", trace.link_ids, trace.folded)
    if not clause:
        return
    if _table(session, "audit_trail"):
        session.execute(
            text(f"""
                UPDATE public.audit_trail SET personal = NULL, source_ip = NULL
                WHERE {clause}
                  AND (personal IS NOT NULL OR source_ip IS NOT NULL)
                """),
            params,
        )
    held, held_params = _invite_match(
        "beside->'personal'->>'email'", trace.link_ids, trace.folded
    )
    if held and _table(session, "audit_trail_held"):
        session.execute(
            text(f"""
                UPDATE public.audit_trail_held
                SET beside = beside - 'personal' - 'source_ip'
                WHERE {held}
                  AND (beside ? 'personal' OR beside ? 'source_ip')
                """),
            held_params,
        )


def _invite_dirty(session, trace: _InviteTrace) -> bool:
    clause, params = _invite_match("personal->>'email'", trace.link_ids, trace.folded)
    if clause and _table(session, "audit_trail"):
        if session.execute(
            text(
                "SELECT 1 FROM public.audit_trail"
                f" WHERE {clause} AND (personal IS NOT NULL OR source_ip IS NOT NULL)"
                " LIMIT 1"
            ),
            params,
        ).first():
            return True
    held, held_params = _invite_match(
        "beside->'personal'->>'email'", trace.link_ids, trace.folded
    )
    if held and _table(session, "audit_trail_held"):
        if session.execute(
            text(
                "SELECT 1 FROM public.audit_trail_held WHERE "
                f"{held} AND (beside ? 'personal' OR beside ? 'source_ip') LIMIT 1"
            ),
            held_params,
        ).first():
            return True
    return False


def scrub_invites(session, principal_ids: list[str], *, notices: bool) -> None:
    """Clear invite traces for these principals.

    Closes an invite still open for an address they hold, then nulls the
    address and source address on ``invite.sent``, ``invite.failed``,
    ``invite.expired`` and ``invite.revoked``. ``notices`` deletes the
    inviter's invite notices; an upgrade leaves them, because nothing left in
    the database ties those rows to the person. Plain SQL on the caller's
    connection: the upgrade runs it too. Running it again changes nothing.
    """
    traces = [_invite_trace(session, principal_id) for principal_id in principal_ids]
    if notices:
        for trace in traces:
            _drop_invite_notices(session, trace)
    closed = _lock_and_close(session, traces)
    merged = _InviteTrace(
        emails=tuple(sorted({email for trace in traces for email in trace.emails})),
        folded=tuple(sorted({email for trace in traces for email in trace.folded})),
        usernames=(),
        link_ids=tuple(
            sorted({*closed, *(link for trace in traces for link in trace.link_ids)})
        ),
    )
    # Close writes the address into the new entries. Blank after that, and
    # only once this transaction holds the trail lock: the leader takes the
    # lock and then locks window rows.
    if closed or _invite_dirty(session, merged):
        trail._lock(session)
        _blank_invite_rows(session, merged)


def _perform(session, principal_id: str) -> None:
    # Lock order, everywhere: the feed row, then the trail lock, then window or
    # retention rows. The caller holds the feed row. The beside-columns wait
    # until this transaction holds the trail lock: the leader takes that lock
    # and then locks window rows, so updating those rows first deadlocks.
    # Invite rows are locked before that, inside the scrub: issuing an invite
    # locks the open invite and then appends.
    _blank_label(session, principal_id)
    # Before _revoke rewrites the username and email, and before the
    # beside-columns that still hold an address are cleared.
    scrub_invites(session, [principal_id], notices=True)
    _revoke_and_drop(session, principal_id)
    trail._lock(session)
    _blank_beside(session, principal_id)
    _queue_append(session, [principal_id])


# Columns that keep a person's username as text, rewritten with the account.
_USERNAME_COLUMNS = (
    ("cases", "assignee"),
    ("case_tasks", "assignee"),
    ("case_comments", "author"),
    ("case_evidence", "collected_by"),
    ("case_relationships", "created_by"),
    ("case_closure_info", "closed_by"),
    ("case_closure_info", "closed_by_text"),
    ("case_attachments", "uploaded_by"),
    ("case_escalations", "escalated_from"),
    ("case_escalations", "escalated_to"),
    ("case_audit_logs", "changed_by"),
    ("ip_exclusions", "created_by"),
    ("ip_exclusions", "removed_by"),
    ("findings", "noise_marked_by"),
    ("detection_proposals", "created_by"),
    ("detection_proposal_events", "actor"),
    ("detection_rules", "linked_by"),
    ("detection_rule_stages", "actor"),
    ("detection_rule_exceptions", "created_by"),
    ("detection_rule_exceptions", "removed_by"),
    ("approval_actions", "created_by"),
    ("approval_actions", "approved_by"),
    ("workflow_runs", "triggered_by"),
    ("custom_workflows", "created_by"),
    ("custom_agents", "created_by"),
    ("episodic_notes", "written_by"),
    ("agent_directives", "actor"),
    ("investigation_replay", "started_by"),
    ("skill_scans", "approved_by"),
    ("skill_installs", "installed_by"),
    ("tool_effect_classes", "classified_by"),
    ("system_config", "updated_by"),
    ("integration_configs", "updated_by"),
    ("ai_model_configs", "updated_by"),
    ("config_audit_log", "changed_by"),
)


def _columns(session, table: str) -> set[str]:
    return set(
        session.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = :table"
            ),
            {"table": table},
        ).scalars()
    )


# Rendered into the case shell's view; raw UPDATEs skip ORM onupdate.
_CASE_VIEW_NAMES = (
    ("cases", "assignee"),
    ("case_tasks", "assignee"),
    ("case_evidence", "collected_by"),
    ("case_escalations", "escalated_from"),
    ("case_escalations", "escalated_to"),
)


def _touch_named_cases(session, params: dict, have: dict[str, set[str]]) -> None:
    """Bump updated_at on every case whose view shows the name, before it is
    rewritten, so a held render of it is not served (#5243)."""
    for table, _ in _CASE_VIEW_NAMES:
        if table not in have:
            have[table] = _columns(session, table)
    if "updated_at" not in have["cases"]:
        return
    # {table} and {column} are fixed names from the list above.
    named = [
        f"SELECT case_id FROM public.{table} WHERE {column} = :old"
        for table, column in _CASE_VIEW_NAMES
        if column in have[table] and "case_id" in have[table]
    ]
    if named:
        session.execute(
            text(
                "UPDATE public.cases SET updated_at = now() AT TIME ZONE 'UTC'"
                f" WHERE case_id IN ({' UNION '.join(named)})"
            ),
            params,
        )


def _rename_records(session, username: str, erased: str) -> None:
    """Rewrite ``username`` to ``erased`` wherever a record keeps it as text.
    An upgrade step runs this on an older schema, so a missing column is skipped."""
    params = {"old": username, "new": erased}
    have: dict[str, set[str]] = {}
    _touch_named_cases(session, params, have)
    for table, column in _USERNAME_COLUMNS:
        if table not in have:
            have[table] = _columns(session, table)
        if column in have[table]:
            # {table} and {column} are fixed names from the list above.
            session.execute(
                text(
                    f"UPDATE public.{table} SET {column} = :new WHERE {column} = :old"
                ),
                params,
            )
    if "mentions" in _columns(session, "case_comments"):
        session.execute(
            text("""
                UPDATE public.case_comments
                SET mentions = array_replace(mentions, CAST(:old AS varchar), CAST(:new AS varchar))
                WHERE CAST(:old AS varchar) = ANY(mentions)
                """),
            params,
        )
    if "chain_of_custody" in _columns(session, "case_evidence"):
        session.execute(
            text("""
                UPDATE public.case_evidence SET chain_of_custody = (
                    SELECT jsonb_agg(CASE WHEN e->>'user' = :old
                        THEN jsonb_set(e, '{user}', to_jsonb(CAST(:new AS text)))
                        ELSE e END ORDER BY n)
                    FROM jsonb_array_elements(chain_of_custody) WITH ORDINALITY AS c(e, n)
                )
                WHERE jsonb_typeof(chain_of_custody) = 'array'
                  AND chain_of_custody @> jsonb_build_array(jsonb_build_object('user', CAST(:old AS text)))
                """),
            params,
        )
    if "value" in _columns(session, "system_config"):
        # The setup wizard names who finished it.
        session.execute(
            text("""
                UPDATE public.system_config
                SET value = jsonb_set(value, '{finished_by}', to_jsonb(CAST(:new AS text)))
                WHERE jsonb_typeof(value) = 'object' AND value->>'finished_by' = :old
                """),
            params,
        )
    # Watching and notices addressed by username serve nobody once the account goes.
    for table in ("case_watchers", "case_notifications"):
        if "user_id" in _columns(session, table):
            session.execute(
                text(f"DELETE FROM public.{table} WHERE user_id = :old"), params
            )


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
    if _table(session, "sign_ins"):
        # The rows and their ids stay; only the device details go.
        session.execute(
            text(
                "UPDATE public.sign_ins SET user_agent = NULL, device = NULL,"
                " address = NULL WHERE principal_id = :id"
            ),
            {"id": principal_id},
        )


def _revoke_and_drop(session, principal_id: str, *, reserve: bool = True) -> None:
    """Revoke the account, then delete the notices erasure removes.

    The username is read before ``_revoke`` rewrites it. Erasure and the
    restore re-apply both call this, so a notice step added here runs on both.
    Only the current name: an earlier one the invite scrub collected is not
    dated, so those notices stay. The user row stays locked by the update
    through the end of this transaction. A notice inserted after that lock
    sees the placeholder.
    """
    username = _current_username(session, principal_id)
    _revoke(session, principal_id, reserve=reserve)
    _drop_notices(session, principal_id)
    _drop_subject_notices(session, principal_id, username)


def _drop_notices(session, principal_id: str) -> None:
    """Delete the notices addressed to this person: their text can name them."""
    if _table(session, "case_notifications"):
        session.execute(
            text("DELETE FROM public.case_notifications WHERE user_id = :id"),
            {"id": principal_id},
        )


def _current_username(session, principal_id: str) -> Optional[str]:
    if not _table(session, "users"):
        return None
    return session.execute(
        text("SELECT username FROM public.users WHERE principal_id = :id"),
        {"id": principal_id},
    ).scalar()


def _user_created_has(session, username: str, principal_id: str, *, own: bool) -> bool:
    """True when a ``user.created`` entry records ``username``.

    ``own`` is this principal. Otherwise any other principal. A held entry
    counts: it is still the creation record. The directory-sync account's
    entry carries no username, so it never matches.
    """
    principal = (
        "payload->>'principal' = :id"
        if own
        else "payload->>'principal' IS NOT NULL AND payload->>'principal' <> :id"
    )
    params = {"id": principal_id, "username": username, "action": "user.created"}
    if (
        _table(session, "audit_trail")
        and session.execute(
            text(f"""
            SELECT 1 FROM public.audit_trail
            WHERE payload->>'action' = :action
              AND personal->>'username' = :username
              AND {principal}
            LIMIT 1
            """),
            params,
        ).first()
    ):
        return True
    if (
        _table(session, "audit_trail_held")
        and session.execute(
            text(f"""
            SELECT 1 FROM public.audit_trail_held
            WHERE payload->>'action' = :action
              AND beside->'personal'->>'username' = :username
              AND {principal}
            LIMIT 1
            """),
            params,
        ).first()
    ):
        return True
    return False


def _subject_clause(username: str) -> tuple[str, dict]:
    # The slots live with the bell. Importing them here, not from notices into
    # a domain, keeps the platform from reaching everything this module imports.
    from core.platform.notices import (
        RUNS_STOPPED_OPENING,
        SUBJECT_TITLES,
        fill_subject,
    )

    parts: list[str] = []
    params: dict[str, Any] = {}
    for kind, templates in SUBJECT_TITLES.items():
        filled = [fill_subject(item, username) for item in templates]
        params[f"kind_{kind}"] = kind
        if len(filled) == 1:
            params[f"title_{kind}"] = filled[0]
            parts.append(
                f"(n.notification_type = :kind_{kind} AND n.title = :title_{kind})"
            )
        else:
            params[f"titles_{kind}"] = filled
            parts.append(
                f"(n.notification_type = :kind_{kind}"
                f" AND n.title = ANY(CAST(:titles_{kind} AS text[])))"
            )
    params["runs_prefix"] = fill_subject(RUNS_STOPPED_OPENING, username)
    parts.append(
        "(n.notification_type = 'runs_stopped'"
        " AND left(n.message, char_length(:runs_prefix)) = :runs_prefix)"
    )
    return " OR ".join(parts), params


def _drop_subject_notices(session, principal_id: str, username: Optional[str]) -> None:
    """Delete bell notices about this person under their current username.

    Rows written before the account existed stay. When this account's
    ``user.created`` entry still has the name, it is dated from the account.
    When a directory gave them the name, the notices stay if another account's
    creation entry has it: the rename is not on the trail, so the scrub cannot
    tell whose notice it is. Nothing here is an upgrade, and nothing calls the
    key ring.
    """
    if (
        not username
        or is_erased(username, principal_id)
        or not _table(session, "case_notifications")
        or not _table(session, "users")
    ):
        return
    if not _user_created_has(session, username, principal_id, own=True):
        if _user_created_has(session, username, principal_id, own=False):
            return
    clause, params = _subject_clause(username)
    params["id"] = principal_id
    session.execute(
        text(f"""
            DELETE FROM public.case_notifications AS n
            USING public.users AS u
            WHERE u.principal_id = :id
              AND n.case_id IS NULL
              AND n.created_at >= u.created_at
              AND ({clause})
            """),
        params,
    )


def _leave_mapped_teams(session, principal_id: str, user_id: str) -> None:
    """Drop synced-team membership. A hand remove of that team is refused."""
    if not _table(session, "team_memberships"):
        return
    # Imported here: teams pulls auth, and auth imports this module.
    from core import teams

    mapped = teams.mapped_team_ids(session)
    if not mapped:
        return
    held = session.execute(
        text("SELECT team_id FROM public.team_memberships WHERE principal_id = :id"),
        {"id": principal_id},
    ).scalars()
    for team_id in list(held):
        if team_id in mapped:
            teams.remove_member(
                session, team_id, user_id, actor=trail.SYSTEM, by_mapping=True
            )


def _revoke(session, principal_id: str, *, reserve: bool = True) -> None:
    """Deactivate the account, keep its username and email as keyed hashes
    (``reserve``), free them, revoke MCP credentials
    and blank their labels, drop its preferences, and end sign-ins, and keep the
    other rows.

    The user row stays, and the principal is not marked deleted. Deactivating
    is what stops the password signing in again and the role being honoured.
    """
    if not _table(session, "users"):
        return
    # Pushes waiting for a link to this account carry its names: read them first.
    names = session.execute(
        text(
            "SELECT username, email, service_account FROM public.users"
            " WHERE principal_id = :id"
        ),
        {"id": principal_id},
    ).first()
    # A service account's names are made up, and an erased one is already placeholders.
    if (
        names
        and reserve
        and not names.service_account
        and not is_erased(names.username, principal_id)
    ):
        identity.reserve(session, names.username, names.email)
    if names and _table(session, "directory_pending"):
        session.execute(
            text("""
                DELETE FROM public.directory_pending
                WHERE lower(username) IN (lower(:u), lower(:e))
                   OR lower(email) IN (lower(:u), lower(:e))
                """),
            {"u": names.username, "e": names.email},
        )
    free_identifiers(session, principal_id)
    user_id = session.execute(
        text(
            "UPDATE public.users SET is_active = false"
            " WHERE principal_id = :id RETURNING user_id"
        ),
        {"id": principal_id},
    ).scalar()
    if not user_id:
        return
    for credential in mcp_credential_service.list_for_user(user_id, session=session):
        mcp_credential_service.revoke(credential.credential_id, session=session)
    drop_personal_rows(session, user_id)
    sign_ins.end_all_for_user(user_id, sign_ins.ERASED, session=session)
    # A synced team refuses a hand remove. Leave it here, while the user row
    # still answers remove_member, before the group rows that named them are gone.
    _leave_mapped_teams(session, principal_id, user_id)
    for table in ("security_keys", "sso_links", "directory_group_members"):
        if _table(session, table):
            # {table} is one of the fixed names above.
            session.execute(
                text(f"DELETE FROM public.{table} WHERE principal_id = :id"),
                {"id": principal_id},
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


def _check_writable() -> None:
    """Refuse before blanking when the list can't be written after the commit.

    A blanking whose id never reaches the list is undone by a restore.
    """
    path = outside_path()
    if path is None:
        raise RetentionError(UNSET_MESSAGE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8"):
            pass
    except OSError as exc:
        raise RetentionError(f"the erasure list is not writable: {path}") from exc


def _check_key() -> None:
    """Refuse before blanking when the username and email can't be hashed: the
    originals are overwritten, so nothing could be hashed later."""
    try:
        key_ring.current("identifier")
    except key_ring.KeyRingError as exc:
        raise RetentionError(f"the identifier key can't be read: {exc}") from exc


def _blocked() -> Optional[str]:
    """Why an erasure can't be recorded now, or None."""
    try:
        _check_writable()
        _check_key()
    except RetentionError as exc:
        return str(exc)
    return None


@contextmanager
def _list_lock():
    """Serialize list writes across replicas, even on different nodes.

    The writes run in ``after_commit``, where the session can't emit SQL, so
    the lock is session-level on its own connection.
    """
    manager = get_db_manager()
    manager.initialize()  # a no-op once the process has its engine
    with manager.engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _LIST_LOCK})
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LIST_LOCK})


def _write_exact(body: str) -> None:
    path = outside_path()
    if path is None:
        raise RetentionError(UNSET_MESSAGE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _list_lock(), path.open("w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())


def _append_file(ids: list[str]) -> None:
    path = outside_path()
    if path is None:
        raise RetentionError(UNSET_MESSAGE)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Read, check and append under one lock: another replica's append lands
    # before the read or after the fsync, never between.
    with _list_lock():
        existing = set(read_ids(path))
        fresh = [pid for pid in dict.fromkeys(ids) if pid not in existing]
        if not fresh:
            return
        with path.open("a", encoding="utf-8") as handle:
            for pid in fresh:
                handle.write(pid + "\n")
            handle.flush()
            os.fsync(handle.fileno())


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


def _managers(
    session, *, active_only: bool, permission: str = "audit.manage"
) -> set[str]:
    """Principals whose role holds ``permission`` (``audit.manage``).

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
              AND (r.permissions -> :permission) = 'true'::jsonb
            """),
        {"person": principals.PERSON, "permission": permission},
    ).scalars()
    found = set(rows)
    if active_only:
        # Blanked on an earlier commit. Erasure deactivates the account, but
        # an admin may reactivate it; the erasure file is what records that
        # they no longer count as a remaining audit manager.
        found.difference_update(read_ids(outside_path()))
    return found


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
        # Live, not stored: why a due erasure would wait.
        "blocked": _blocked(),
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
    """The feed row, before the trail lock and before window or retention rows.

    The one lock order: feed row, then the trail lock, then window or retention
    rows. ``maintenance_and_send`` takes them so, and so must every save, export
    and erasure that appends (the insert takes the trail lock); taking the trail
    lock first deadlocks with that tick or with another holder of the feed row.
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
