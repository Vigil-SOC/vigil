"""Who hears about Medic (#4340, #4366).

The watch lives in ``core.platform``, which must not import ``core.auth``.
This module is the other way round: it names the holders and registers the
bell lines.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import func

from core.auth.permissions import MEDIC_ADMIN_PERMISSION, holders, missing_permission
from core.auth.service_account import is_medic
from core.auth.token_expiry import MEDIC_LINK
from core.platform import medic_last_seen, notices
from core.platform.medic_status import medic_enabled
from core.storage.models import Principal, User
from core.storage.unit_of_work import unit_of_work


def not_watching(user) -> list[dict[str, str]]:
    """Shown to a current holder of ``medic.admin`` while the announced word is red."""
    if missing_permission(user.user_id, MEDIC_ADMIN_PERMISSION) is not None:
        return []
    line = medic_last_seen.not_watching_line()
    return [line] if line else []


def problems_open(user) -> list[dict[str, str]]:
    """Shown to a current holder of ``medic.admin`` while in-scope incidents are open."""
    if missing_permission(user.user_id, MEDIC_ADMIN_PERMISSION) is not None:
        return []
    line = medic_last_seen.problems_open_line()
    return [line] if line else []


def password_site(user) -> list[dict[str, str]]:
    """Shown to a current holder of ``medic.admin`` while Medic's account still has
    a password (#4486). Clears itself when the backend ends the password on the
    gateway's first token read (``current_user._end_medic_password``)."""
    if missing_permission(user.user_id, MEDIC_ADMIN_PERMISSION) is not None:
        return []
    if not medic_enabled():
        return []
    with unit_of_work() as session:
        # Same predicates as service_account._retire_others; only the name leaves.
        username = (
            session.query(User.username)
            .filter(
                User.service_account.is_(True),
                User.username.like("medic-%"),
                func.length(User.username) == 18,  # medic- + 12, as USERNAME_PATTERN
                User.is_active.is_(True),
                User.password_hash.isnot(None),
                User.principal_id.in_(
                    session.query(Principal.principal_id).filter(
                        Principal.deleted_at.is_(None)
                    )
                ),
            )
            .limit(1)
            .scalar()
        )
    if username and is_medic(SimpleNamespace(service_account=True, username=username)):
        return [
            {
                "message": "Medic still reads with a password. Move it to a token "
                "from the host: scripts/medic/enable-compose.sh, or "
                f"python -m core.auth.service_account token {username}",
                "link": MEDIC_LINK,
            }
        ]
    return []


def register() -> None:
    medic_last_seen.bind_medic_admin_holders(lambda: holders(MEDIC_ADMIN_PERMISSION))
    notices.CONDITIONS.append(not_watching)
    notices.CONDITIONS.append(problems_open)
    notices.CONDITIONS.append(password_site)
