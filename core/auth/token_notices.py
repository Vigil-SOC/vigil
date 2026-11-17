"""Who is told about an API token, and the notices a lowered maximum sends.

One recipient rule for every token notice: the holder; for a service account its
accountable person while they are active, otherwise the account managers; for
Medic's token also the holders of ``medic.admin``. A notice never carries the
token itself, because notices are emailed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from core.platform.notices import notify
from core.storage.models import McpCredential, User

logger = logging.getLogger(__name__)

LINK = "/settings?section=users"


def medic_renewal(username: str) -> str:
    """How Medic's token is renewed, on each install shape (docs/medic/enable.md)."""
    return (
        "Renew it from the host. Docker Compose: scripts/medic/enable-compose.sh "
        f"--rotate. Helm: run python -m core.auth.service_account token {username} "
        "in the backend and pipe its output into the gateway's token Secret "
        "(kubectl create secret generic <Secret> --from-file=<key>=/dev/stdin "
        "--dry-run=client -o yaml | kubectl apply -f -); `helm get notes <release>` "
        "prints the exact command"
    )


def recipients(holder: User, session: Optional[Session] = None) -> list[str]:
    """Principal ids to tell about ``holder``'s token, without repeats."""
    from core.auth.auth_service import _manager_rows
    from core.auth.permissions import MEDIC_ADMIN_PERMISSION, holders
    from core.auth.service_account import is_medic
    from core.storage.unit_of_work import unit_of_work

    if holder.service_account is not True:
        return [holder.principal_id]
    with unit_of_work(session) as session:
        accountable = holder.accountable_principal_id
        active = accountable is not None and (
            session.query(User.user_id)
            .filter(User.principal_id == accountable, User.is_active.is_(True))
            .first()
            is not None
        )
        found = (
            [accountable]
            if active
            else [principal for principal, _, _ in _manager_rows("", session)]
        )
    if is_medic(holder):
        found += holders(MEDIC_ADMIN_PERMISSION)
    return list(dict.fromkeys(found))


def tell(
    holder: User,
    title: str,
    message: str,
    priority: str = "normal",
    session: Optional[Session] = None,
) -> list[int]:
    """Tell each recipient of ``holder``'s token. With ``session`` the notices
    are only added: pass the ids to ``send_notices`` after the commit."""
    from core.auth.auth_service import with_dispute

    ids = []
    for to in recipients(holder, session):
        try:
            text = with_dispute(to, message, "", session)
        except Exception:  # noqa: BLE001 - the notice still goes, in today's wording
            logger.exception("could not name the managers on a token notice")
            text = message
        try:
            notice = notify(
                to,
                "api_token",
                title,
                text,
                link=LINK,
                priority=priority,
                session=session,
            )
        except Exception:  # noqa: BLE001 - the act stands; the notice is not its record
            logger.exception("Could not notify about a token change")
            continue
        if notice is not None:
            ids.append(notice)
    return ids


def _date(value: Optional[datetime]) -> str:
    return value.strftime("%Y-%m-%d") if value else "never"


def tell_shortened(
    rows: list[tuple[McpCredential, User]],
    ends: dict[str, datetime],
    session: Session,
) -> list[int]:
    """One notice per recipient, listing the tokens a lowered maximum ended early."""
    from core.auth.service_account import is_medic

    lines: dict[str, list[str]] = {}
    renew: dict[str, list[str]] = {}
    for credential, holder in rows:
        line = (
            f'"{credential.label}"'
            + (
                f" (service account {holder.username})"
                if holder.service_account
                else ""
            )
            + f" now ends on {_date(ends[credential.credential_id])}, "
            f"not {_date(credential.expires_at)}"
        )
        for to in recipients(holder, session):
            lines.setdefault(to, []).append(line)
            if is_medic(holder):
                renew.setdefault(to, []).append(
                    f"Medic's token ({holder.username}): {medic_renewal(holder.username)}"
                )
    ids = []
    for to, items in lines.items():
        notice = notify(
            to,
            "api_token",
            "API tokens shortened",
            "The maximum lifetime for API tokens was lowered, so existing tokens "
            "end sooner: "
            + "; ".join(items)
            + "."
            + "".join(f" {text}." for text in renew.get(to, [])),
            link=LINK,
            priority="high",
            session=session,
        )
        if notice is not None:
            ids.append(notice)
    return ids


def describe(credential: McpCredential, holder: User) -> str:
    """``The token "label"``, naming the service account when the holder is one."""
    return f'The token "{credential.label}"' + (
        f" of service account {holder.username}" if holder.service_account else ""
    )


def tell_reminded(
    rows: list[tuple[McpCredential, User, datetime]], session: Session
) -> list[int]:
    """One notice per recipient, listing the unused tokens a reminder went out for.
    Each row carries the date its token is revoked unless used."""
    from core.auth.service_account import is_medic

    lines: dict[str, list[str]] = {}
    renew: dict[str, list[str]] = {}
    for credential, holder, ends in rows:
        used = credential.last_used_at
        line = (
            describe(credential, holder)
            + (f" was last used on {_date(used)}" if used else " has never been used")
            + f" and will be revoked on {_date(ends)}"
        )
        for to in recipients(holder, session):
            lines.setdefault(to, []).append(line)
            if is_medic(holder):
                renew.setdefault(to, []).append(
                    f"Medic's token ({holder.username}): {medic_renewal(holder.username)}"
                )
    ids = []
    for to, items in lines.items():
        try:
            notice = notify(
                to,
                "api_token",
                "Unused API tokens will be revoked",
                "These API tokens are unused. "
                + "; ".join(items)
                + ". Using a token once keeps it; otherwise mint a replacement before then."
                + "".join(f" {text}." for text in renew.get(to, [])),
                link=LINK,
                priority="high",
                session=session,
            )
        except Exception:  # noqa: BLE001 - the other recipients are still told
            logger.exception("Could not notify about unused tokens")
            continue
        if notice is not None:
            ids.append(notice)
    return ids
