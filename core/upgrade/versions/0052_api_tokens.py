"""Tokens get a lifetime: none for ever, none past a year (#4223).

An unrevoked token with no expiry, or with more than 365 days left, now expires
90 days from the upgrade. Each is a trail entry naming its new date; the holder
sees it in the Tokens list. A token already inside a year is left alone, so a
second run changes nothing. The report counts the tokens moved.
"""

from __future__ import annotations

from datetime import timedelta

from alembic import op
from sqlalchemy import text

from core.upgrade.revision import run_sql, stash_report

revision = "0052_api_tokens"
down_revision = "0051_erased_identifiers"

MAX_DAYS = 365
NEW_DAYS = 90


def _trail(conn, entries: list[dict]) -> None:
    from core.audit import trail
    from core.version import __version__

    if not entries or not trail.trail_ready(conn):
        return
    for entry in entries:
        trail.append(
            conn,
            {
                trail.ACTION: "credential.expiry_set",
                trail.ACTOR: trail.SYSTEM,
                "release": __version__,
                **entry,
            },
        )


def upgrade() -> None:
    conn = op.get_bind()
    run_sql(__file__)
    now = conn.execute(text("SELECT (now() AT TIME ZONE 'UTC')")).scalar_one()
    moved = conn.execute(
        text("""
            UPDATE public.mcp_credentials AS c
            SET expires_at = :new
            FROM public.users AS u
            WHERE u.user_id = c.user_id
              AND c.revoked_at IS NULL
              AND (c.expires_at IS NULL OR c.expires_at > :limit)
            RETURNING c.credential_id, u.principal_id
            """),
        {
            "new": now + timedelta(days=NEW_DAYS),
            "limit": now + timedelta(days=MAX_DAYS),
        },
    ).all()
    expires = (now + timedelta(days=NEW_DAYS)).isoformat()
    _trail(
        conn,
        [
            {"principal": principal, "credential_id": cid, "expires_at": expires}
            for cid, principal in moved
        ],
    )
    stash_report(revision, {"tokens_expiring": len(moved)})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
