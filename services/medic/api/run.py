"""`POST /v1/incidents/{incident_id}/fix`: one click of the soc-daemon restart.

The body is empty. `who` is `X-Medic-Admin`, the session, never the body.
A check that fails is 409 `fix_refused` with that check as `reason`; the
fix_run is already in the log and nothing was restarted.
"""

from __future__ import annotations

import json

from services.medic.api.decisions import _INCIDENT, Problem, _Bad, _reject_query
from services.medic.api.feedback import _ADMIN
from services.medic.fix import Fixer

MAX_BODY = 4096


def post_fix(
    fixer: Fixer | None,
    incident_id: str,
    query: str,
    *,
    content_type: str | None,
    length: str | None,
    admins: list[str],
    read_body,
) -> dict | Problem:
    try:
        _reject_query(query)
        if _INCIDENT.fullmatch(incident_id) is None:
            raise _Bad("invalid_parameter", "incident_id")
    except _Bad as bad:
        return bad.problem
    if length is None or not length.isascii() or not length.isdigit():
        return Problem(400, "invalid_parameter", "Content-Length")
    size = int(length)
    if size > MAX_BODY:
        return Problem(413, "payload_too_large")
    if size:
        media = (content_type or "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            return Problem(415, "unsupported_media_type")
        try:
            doc = json.loads(read_body(size).decode("utf-8"))
        except (ValueError, RecursionError):
            return Problem(422, "invalid_body")
        if doc != {}:
            return Problem(422, "invalid_body")
    if len(admins) != 1 or _ADMIN.fullmatch(admins[0]) is None:
        return Problem(422, "missing_admin_identity", "X-Medic-Admin")
    if fixer is None:
        return Problem(503, "busy")
    return fixer.run(incident_id, admins[0])
