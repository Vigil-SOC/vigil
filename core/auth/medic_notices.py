"""Who hears that Medic stopped watching (#4340).

The watch lives in ``core.platform``, which must not import ``core.auth``.
This module is the other way round: it names the holders and registers the
bell line.
"""

from __future__ import annotations

from core.auth.permissions import MEDIC_ADMIN_PERMISSION, holders, missing_permission
from core.platform import medic_last_seen, notices


def not_watching(user) -> list[dict[str, str]]:
    """Shown to a current holder of ``medic.admin`` while the announced word is red."""
    if missing_permission(user.user_id, MEDIC_ADMIN_PERMISSION) is not None:
        return []
    line = medic_last_seen.not_watching_line()
    return [line] if line else []


def register() -> None:
    medic_last_seen.bind_medic_admin_holders(lambda: holders(MEDIC_ADMIN_PERMISSION))
    notices.CONDITIONS.append(not_watching)
