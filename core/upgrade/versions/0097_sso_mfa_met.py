"""SSO sign-ins meet Require MFA only on the provider's signed claim (#4757).

Adds ``sign_ins.mfa_met`` and ``sign_ins.ends_at``. From the upgrade on, a
covered person who signs in only through the provider is asked for their Vigil
factor, or held at setup, unless the provider's ID token says MFA happened. The
report counts the active people that changes, split by whether they hold a
Vigil factor (asked for it) or not (held at setup unless the provider sends a
qualifying claim). Open sign-ins are left to run to their usual end.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.upgrade.revision import run_sql, stash_report

revision = "0097_sso_mfa_met"
down_revision = "0096_entity_facts"


def upgrade() -> None:
    run_sql(__file__)
    conn = op.get_bind()
    report = {"sso_only_with_factor": 0, "sso_only_without_factor": 0}
    # No link, no one signs in through the provider: nothing to count, and the
    # settings reads below are skipped on a fresh install.
    if conn.execute(text("SELECT 1 FROM public.sso_links LIMIT 1")).first():
        from core.auth import session_policy, sso, sso_settings

        def stored(key: str) -> dict:
            value = conn.execute(
                text("SELECT value FROM public.system_config WHERE key = :k"),
                {"k": key},
            ).scalar()
            return value if isinstance(value, dict) else {}

        level = stored(session_policy.KEY).get(
            "require_mfa", session_policy.SessionPolicy().require_mfa
        )
        # Only Require single sign-on is read, so a hand-edited row can't fail this.
        settings = sso_settings.SsoSettings(
            require_sso=stored(sso_settings.KEY).get("require_sso") is True
        )
        # Plain SQL for the accounts: a later revision's columns don't exist yet.
        rows = conn.execute(text("""
            SELECT u.principal_id, u.password_hash, u.break_glass_at,
                   u.service_account, u.mfa_enabled, r.permissions
            FROM public.users AS u
            LEFT JOIN public.roles AS r ON r.role_id = u.role_id
            WHERE u.is_active
            """)).all()
        session = Session(bind=conn)
        for row in rows:
            held = [name for name, on in (row.permissions or {}).items() if on]
            if session_policy.mfa_covered(
                held, row.service_account is True, level
            ) and sso.sso_only(session, row, settings):
                key = "with" if row.mfa_enabled else "without"
                report[f"sso_only_{key}_factor"] += 1
        session.close()
    stash_report(revision, report)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
