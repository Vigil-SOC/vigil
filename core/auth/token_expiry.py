"""A live warning for API tokens about to expire (#4223).

Computed on each request from ``mcp_credentials`` and stored nowhere: it clears
itself once the token is rotated (it has a successor), revoked or expired. The
holder sees their own; a holder of ``users.write`` also sees a service account's.
"""

from __future__ import annotations

from datetime import timedelta

from core.platform import notices
from core.storage.models import McpCredential, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

WARN_DAYS = 14
LINK = "/settings?section=users"


def warning(user: User) -> list[dict[str, str]]:
    from core.auth.permissions import missing_permission

    now = utcnow()
    with unit_of_work() as session:
        rows = (
            session.query(McpCredential, User.service_account)
            .join(User, User.user_id == McpCredential.user_id)
            .filter(
                McpCredential.revoked_at.is_(None),
                McpCredential.successor_id.is_(None),
                McpCredential.expires_at > now,
                McpCredential.expires_at <= now + timedelta(days=WARN_DAYS),
            )
            .order_by(McpCredential.expires_at)
            .all()
        )
        # Looked up only when there is a service account's token to show.
        manages = None
        out = []
        for credential, machine in rows:
            if credential.user_id != user.user_id:
                if machine is not True:
                    continue
                if manages is None:
                    manages = missing_permission(user.user_id, "users.write") is None
                if not manages:
                    continue
            days = max((credential.expires_at - now).days, 0)
            when = "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
            whose = "" if credential.user_id == user.user_id else " (a service account's)"
            out.append(
                {
                    "message": f'API token "{credential.label}"{whose} expires {when}',
                    "link": LINK,
                }
            )
        return out


def register() -> None:
    notices.CONDITIONS.append(warning)
