"""A live warning for API tokens about to expire (#4223).

Computed on each request from ``mcp_credentials`` and stored nowhere: it clears
itself once the token is rotated (it has a successor), revoked or expired. The
holder sees their own; a holder of ``users.write``, and the person accountable for
it, also see a service account's. Medic's token also reaches ``medic.admin``
holders, and names the host commands that renew it, for Compose and for Helm.

The warning starts 14 days before the end, or half the token's lifetime if that is
shorter, so a low maximum does not leave every token in a standing warning.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from core.auth.token_notices import medic_renewal
from core.platform import notices
from core.storage.models import McpCredential, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

WARN_DAYS = 14
WARN_LEAD = timedelta(days=WARN_DAYS)
LINK = "/settings?section=users"
MEDIC_LINK = "/medic"  # where medic.admin holders see Medic's notices


def warning(user: User) -> list[dict[str, str]]:
    from core.auth.permissions import MEDIC_ADMIN_PERMISSION, missing_permission
    from core.auth.service_account import is_medic

    now = utcnow()
    with unit_of_work() as session:
        rows = (
            session.query(
                McpCredential,
                User.service_account,
                User.accountable_principal_id,
                User.username,
            )
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
        manages = medic_admin = None
        out = []
        for credential, machine, accountable, username in rows:
            lifetime = credential.expires_at - credential.created_at
            if credential.expires_at - now > min(WARN_LEAD, lifetime / 2):
                continue
            medic = is_medic(
                SimpleNamespace(service_account=machine, username=username)
            )
            if credential.user_id != user.user_id:
                if machine is not True:
                    continue
                if accountable != user.principal_id:
                    if manages is None:
                        manages = (
                            missing_permission(user.user_id, "users.write") is None
                        )
                    if medic and medic_admin is None:
                        medic_admin = (
                            missing_permission(user.user_id, MEDIC_ADMIN_PERMISSION)
                            is None
                        )
                    if not (manages or (medic and medic_admin)):
                        continue
            days = max((credential.expires_at - now).days, 0)
            when = "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
            if medic:
                out.append(
                    {
                        "message": f"Medic's API token expires {when}. "
                        + medic_renewal(username),
                        "link": MEDIC_LINK if medic_admin else LINK,
                    }
                )
                continue
            whose = (
                "" if credential.user_id == user.user_id else " (a service account's)"
            )
            if credential.oauth_client_id:
                # An OAuth grant can't be renewed; the person reconnects the app.
                message = (
                    f'Connected app "{credential.label}"{whose} expires {when}; '
                    "reconnect it from the app to keep access"
                )
            else:
                message = f'API token "{credential.label}"{whose} expires {when}'
            out.append({"message": message, "link": LINK})
        return out


def register() -> None:
    notices.CONDITIONS.append(warning)
