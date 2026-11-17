"""Tokens left unused are reminded, then revoked (#4725).

Adds ``mcp_credentials.reminded_at``. Nothing is reminded or revoked by the
upgrade itself: the first sweep reminds each token already idle past the period,
and revokes it no sooner than 7 days later unless it is used. The report counts
the tokens that will get that reminder at the default 45 days, as revision 0052
counted the tokens it changed.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from core.upgrade.revision import run_sql, stash_report

revision = "0092_token_reminded_at"
down_revision = "0091_case_finding_removals"

DAYS = 45


def upgrade() -> None:
    run_sql(__file__)
    # Unrevoked, unexpired, not rotated; a never-used token counts from creation.
    # Directory sync's token is left out while sync is on: it is exempt.
    idle = (
        op.get_bind()
        .execute(
            text("""
            SELECT count(*)
            FROM public.mcp_credentials AS c
            JOIN public.users AS u ON u.user_id = c.user_id
            WHERE c.revoked_at IS NULL
              AND c.successor_id IS NULL
              AND (c.expires_at IS NULL OR c.expires_at > (now() AT TIME ZONE 'utc'))
              AND COALESCE(c.last_used_at, c.created_at)
                  <= (now() AT TIME ZONE 'utc') - make_interval(days => :days)
              AND NOT (
                  (u.username = 'directory-sync' OR u.role_id = 'role-directory-sync')
                  AND COALESCE((
                      SELECT (value ->> 'sync_enabled') = 'true'
                      FROM public.system_config WHERE key = 'auth.sso'
                  ), false)
              )
            """),
            {"days": DAYS},
        )
        .scalar_one()
    )
    stash_report(revision, {"tokens_to_remind": idle, "unused_days": DAYS})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
