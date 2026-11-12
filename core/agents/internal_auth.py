# How the TypeScript agent layer proves it is the caller. Shared by every
# /internal endpoint: a security check written twice is one edit from a hole.

from __future__ import annotations

import base64
import hmac
import json
import logging
from typing import Optional

from fastapi import HTTPException

from core.auth import tool_principal
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
def accepted(what: str) -> None:
    _note("ok", "%s accepted the internal token", what)


def authorise(presented: Optional[str], what: str, *, note_ok: bool = True) -> None:
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
        if note_ok:
            accepted(what)
        return
    if not presented:
        _note("missing", "%s refused: no credential sent", what)
    else:
        _note("wrong", "%s refused: wrong credential", what)
    raise HTTPException(status_code=401, detail="bad or missing internal token")


# A principal that does not name the run this call is for. The bearer stays
# 401 and 503; these three are the only 403s, and each is its own latch kind.
MISSING = "run principal missing"
OTHER_RUN = "run principal names another run"
UNVERIFIABLE = "run principal expired or unverifiable"


def _named_run(named: Optional[str], run_id: str) -> Optional[str]:
    """The kind when `named` is not this run. None when it is."""
    if not named:
        return MISSING
    if named != run_id:
        return OTHER_RUN
    return None


def _claimed_run(token: str) -> Optional[str]:
    """The run id a token carries, for the log. Not a check: a bad signature
    still says which run it claimed, and the token itself stays out of the log.
    """
    try:
        segment = token.split(".")[1]
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError, IndexError, UnicodeError, TypeError):
        return None
    run_id = data.get("run_id") if isinstance(data, dict) else None
    if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
        return None
    if any(ord(char) < 32 for char in run_id):
        return None
    return run_id


def _refuse_run(
    kind: str, what: str, run_id: str, principal_run: Optional[str]
) -> None:
    # The run ids only. The token is a credential and does not belong in the log.
    _note(
        kind,
        "%s refused: %s (run %s, principal run %s)",
        what,
        kind,
        run_id,
        principal_run or "none",
    )


def authorise_run(
    what: str,
    run_id: Optional[str],
    principal: Optional[str] = None,
    *,
    authorization: Optional[str] = None,
    claims: Optional[tool_principal.Claims] = None,
) -> Optional[str]:
    """The run this call names must be the run its principal names.

    Path shape (no claims): the shared bearer first, then the run-principal
    header. A bad principal raises 403 and the detail is the kind.

    Invoke shape (claims): the bearer was already checked. A call that names
    no run is left alone. Otherwise the kind, for `_failure("refused")`.
    """
    if claims is not None:
        if not run_id:
            return None
        kind = _named_run(claims.run_id, run_id)
        if kind is None:
            return None
        _refuse_run(kind, what, run_id, claims.run_id)
        return kind

    authorise(authorization, what, note_ok=False)
    path_run = run_id or ""
    if not principal:
        _refuse_run(MISSING, what, path_run, None)
        raise HTTPException(status_code=403, detail=MISSING)
    try:
        read = tool_principal.read(principal)
    except tool_principal.InvalidPrincipal:
        _refuse_run(UNVERIFIABLE, what, path_run, _claimed_run(principal))
        raise HTTPException(status_code=403, detail=UNVERIFIABLE)
    kind = _named_run(read.run_id, path_run)
    if kind is not None:
        _refuse_run(kind, what, path_run, read.run_id)
        raise HTTPException(status_code=403, detail=kind)
    accepted(what)
    return None
