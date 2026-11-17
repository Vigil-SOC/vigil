"""`POST /v1/fix-level`: an admin raises one step, or lowers, a fix type's level.

The body is `{fix_type, level, reason?}`. `who` is `X-Medic-Admin`, the session,
never the body. A raise needs a reason; the reason is K2-redacted like a feedback
comment. A refusal about the install's state is 409 `fix_refused` with `reason`.
"""

from __future__ import annotations

import json
import sqlite3

from services.medic.api.decisions import Problem, _Bad, _reject_query
from services.medic.api.feedback import _ADMIN, _COMMENT_MAX, _excerpt
from services.medic.fix import Fixer
from services.medic.levels import FIX_TYPES, LEVELS
from services.medic.store import StoreError

MAX_BODY = 4096
_FIELDS = {"fix_type", "level", "reason"}


def _invalid(field: str | None = None) -> Problem:
    return Problem(422, "invalid_body", field)


def post_level(
    fixer: Fixer | None,
    query: str,
    *,
    content_type: str | None,
    length: str | None,
    admins: list[str],
    read_body,
) -> dict | Problem:
    try:
        _reject_query(query)
    except _Bad as bad:
        return bad.problem
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        return Problem(415, "unsupported_media_type")
    if length is None or not length.isascii() or not length.isdigit():
        return Problem(400, "invalid_parameter", "Content-Length")
    if int(length) > MAX_BODY:
        return Problem(413, "payload_too_large")
    if len(admins) != 1 or _ADMIN.fullmatch(admins[0]) is None:
        return Problem(422, "missing_admin_identity", "X-Medic-Admin")
    try:
        doc = json.loads(read_body(int(length)).decode("utf-8"))
    except (ValueError, RecursionError):
        return _invalid()
    if not isinstance(doc, dict) or not _FIELDS.issuperset(doc):
        return _invalid()
    if doc.get("fix_type") not in FIX_TYPES:
        return _invalid("/fix_type")
    if doc.get("level") not in LEVELS:
        return _invalid("/level")
    reason = doc.get("reason")
    if reason is not None and (
        not isinstance(reason, str) or not 1 <= len(reason) <= _COMMENT_MAX
    ):
        return _invalid("/reason")
    if fixer is None:
        return Problem(503, "busy")
    try:
        # A reason that redacts to nothing is no reason.
        return fixer.levels.set(
            doc["fix_type"], doc["level"], admins[0], reason and _excerpt(reason)
        )
    except (StoreError, sqlite3.Error):
        return Problem(503, "busy")
