"""Database copy of the per-user session cutoff.

``revoke_all_for_user`` mirrors the same instant into Redis so a lookup can
reject a token before the user row is loaded. Authentication also compares
``iat`` with this column, which is written in the caller's transaction, so a
refused Redis write cannot leave the old sessions valid.
"""

from datetime import datetime, timezone
from typing import Optional

from core.time import utcnow


def stamp_session_cutoff(user) -> None:
    """Mark every token issued before now as finished.

    The caller's transaction commits it with the password, email, or role
    change that required the sign-out.
    """
    user.tokens_valid_after = utcnow()


def issued_before_cutoff(payload: dict, cutoff: Optional[datetime]) -> bool:
    """True when ``payload`` was issued before ``cutoff``, or has no ``iat``."""
    if cutoff is None:
        return False
    iat = payload.get("iat")
    if iat is None:
        return True
    if cutoff.tzinfo is not None:
        cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)
    # Same comparison as the Redis cutoff: a token minted in that second
    # is still accepted, so a login immediately after the change succeeds.
    cutoff_ts = int(cutoff.replace(tzinfo=timezone.utc).timestamp())
    return int(iat) < cutoff_ts
