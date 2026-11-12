"""Whether a person is acting directly: the test every widening of access takes.

Moved here from ``services/api/routers/users.py`` so the case routes (#4354)
take the same test as role and team changes.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from core.auth import current_user as current_user_module
from core.auth import principals
from core.storage.models import Principal, User


def is_service_account(user: User) -> bool:
    # Only an explicit True is one; a user object without the field is not.
    try:
        return user.service_account is True
    except AttributeError:
        return False


def principal_kind(user: User, session: Session) -> Optional[str]:
    return (
        session.query(Principal.kind)
        .filter(Principal.principal_id == user.principal_id)
        .scalar()
    )


def acts_as_person(user: User, session: Session) -> bool:
    """A person's own session. A service account's is not, nor is any API token's;
    DEV_MODE's dev user is."""
    if is_service_account(user) or current_user_module.is_token_request():
        return False
    kind = principal_kind(user, session)
    return kind == principals.PERSON if kind else current_user_module.DEV_MODE
