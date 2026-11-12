# How the TypeScript agent layer proves it is the caller. Shared by every
# /internal endpoint: a security check written twice is one edit from a hole.

from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastapi import HTTPException

from core.secrets import get_secret

logger = logging.getLogger(__name__)

TOKEN_SECRET = "AGENT_INTERNAL_TOKEN"
PREVIOUS_SECRET = "AGENT_INTERNAL_TOKEN_PREVIOUS"

# None until the first call, then "ok" or the refusal kind in progress. Same latch
# as _note_read: one warning when a kind starts, debug while it repeats, one info
# when a call succeeds and this process was not already succeeding.
_auth: Optional[str] = None


def _note(kind: str, message: str, *args: object) -> None:
    global _auth
    if kind == "ok":
        if _auth != "ok":
            logger.info(message, *args)
        _auth = "ok"
        return
    if _auth == kind:
        logger.debug(message, *args)
        return
    _auth = kind
    logger.warning(message, *args)


def _bearer_matches(presented: str, token: str) -> bool:
    # compare_digest rather than !=, which returns on the first differing byte.
    # Encoded because it refuses a str holding anything above U+00FF.
    return hmac.compare_digest(
        presented.encode("utf-8", "surrogatepass"),
        f"Bearer {token}".encode(),
    )


# The token alone, since ADR 0014. This paired with a loopback check until #635
# made the agent layer its own Deployments, at which point every legitimate caller
# arrives from a pod address and the check refused all of them. What replaces it is
# the chart's NetworkPolicy, which names the pods that may connect rather than
# asserting "same box" -- a stronger statement, and one Kubernetes enforces.
#
# Which makes the refusal below load-bearing rather than defensive: it is now the
# only thing between a reachable pod and an open endpoint.
def authorise(presented: Optional[str], what: str) -> None:
    expected = get_secret(TOKEN_SECRET)
    # Told apart deliberately: a deployment that never set the secret reads exactly
    # like a caller with the wrong one, and the fix for each is nothing alike.
    # Previous is ignored until current is set: an empty current stays not configured.
    if not expected:
        _note("unset", "%s refused: %s is not configured", what, TOKEN_SECRET)
        raise HTTPException(status_code=503, detail=f"{TOKEN_SECRET} is not configured")
    previous = get_secret(PREVIOUS_SECRET) or ""
    # Empty, or the same value current already has, is unset. No trimming.
    if previous == expected:
        previous = ""
    presented_value = presented or ""
    # Both comparisons run when previous is set, including after the first matches,
    # so the time taken does not say which value was presented. The calls stay
    # outside `or`, which would skip the second.
    matched_current = _bearer_matches(presented_value, expected)
    matched_previous = _bearer_matches(presented_value, previous) if previous else False
    if matched_current or matched_previous:
        _note("ok", "%s accepted the internal token", what)
        return
    if not presented:
        _note("missing", "%s refused: no credential sent", what)
    else:
        _note("wrong", "%s refused: wrong credential", what)
    raise HTTPException(status_code=401, detail="bad or missing internal token")
