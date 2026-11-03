"""`POST /v1/incidents/{incident_id}/feedback`: the API's only write.

Appends one `feedback` record through the main loop's `DecisionWriter` (the
single writer, C4 §6.7; it is mutex-guarded, so the listener's threads may call
it). `admin` comes only from `X-Medic-Admin`, never the body. The comment is
K2-redacted before it is stored, and neither the raw text nor any detail of a
failure is kept or logged. The check against `rateability` happens before the
write; a race with a state change is accepted, because the fold is the truth.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from services.medic.api.decisions import (
    _INCIDENT,
    _SCHEMA,
    Problem,
    _Bad,
    _field,
    _reject_query,
    _with_store,
)
from services.medic.api.incidents import _FEEDBACK, _folds, rateability
from services.medic.redact import REDACTION_VERSION, Redactor
from services.medic.store import RecordHeld, StoreError
from services.medic.store.writer import DecisionWriter, utc_now

MAX_BODY = 4096
_COMMENT_MAX = 500
_ADMIN = re.compile(_SCHEMA["$defs"]["id"]["pattern"])
_FIELDS = {"value", "suggested_lane", "comment"}
_REDACTOR = Redactor()


def _invalid(field: str | None = None) -> Problem:
    return Problem(422, "invalid_body", field)


def _parse(raw: bytes) -> dict[str, Any] | Problem:
    """The request body as `FeedbackRequest`, or the 422 for its first fault."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):  # includes UnicodeDecodeError
        return _invalid()
    if not isinstance(doc, dict):
        return _invalid()
    for key in doc:
        if key not in _FIELDS:
            return _invalid(_field("/" + key))
    value = doc.get("value")
    if value not in _FEEDBACK or not isinstance(value, str):
        return _invalid("/value")
    if "suggested_lane" in doc:
        lane = doc["suggested_lane"]
        if type(lane) is not int or lane not in (1, 2, 3) or value != "wrong_lane":
            return _invalid("/suggested_lane")
    if "comment" in doc:
        comment = doc["comment"]
        if not isinstance(comment, str) or not 1 <= len(comment) <= _COMMENT_MAX:
            return _invalid("/comment")
    return doc


def _excerpt(comment: str) -> dict[str, str] | None:
    """The stored comment: redacted, cut to 500, or None if nothing is left."""
    text = _REDACTOR.redact(comment)[0][:_COMMENT_MAX]
    if not text.strip():
        return None
    return {"text": text, "redaction_version": REDACTION_VERSION}


def post_feedback(
    data_dir,
    writer: DecisionWriter | None,
    incident_id: str,
    query: str,
    *,
    content_type: str | None,
    length: str | None,
    admins: list[str],
    read_body,
) -> dict | Problem:
    """`read_body(n)` reads the body, called only once every cheaper check has
    passed. Returns `{"record": ...}` (201) or the Problem to answer with."""
    try:
        _reject_query(query)
        if _INCIDENT.fullmatch(incident_id) is None:
            raise _Bad("invalid_parameter", "incident_id")
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
    doc = _parse(read_body(int(length)))
    if isinstance(doc, Problem):
        return doc
    if writer is None:
        return Problem(503, "busy")

    def check(conn: sqlite3.Connection) -> Problem | None:
        folds, children = _folds(conn)
        fold = folds.get(incident_id)
        if fold is None:
            return Problem(404, "not_found")
        summary = fold.summary(children.get(incident_id, []))
        _, why = rateability(summary)
        if why is None and doc["value"] not in summary["allowed_feedback"]:
            why = "value_not_offered"
        return Problem(409, "not_rateable", reason=why) if why else None

    refused = _with_store(data_dir, check)
    if refused is not None:
        return refused
    if writer.stopped:
        return Problem(503, "store_not_recording")
    body: dict[str, Any] = {
        "incident_id": incident_id,
        "value": doc["value"],
        "source": "live",
        "admin": admins[0],
    }
    if "suggested_lane" in doc:
        body["suggested_lane"] = doc["suggested_lane"]
    if "comment" in doc and (excerpt := _excerpt(doc["comment"])):
        body["comment"] = excerpt
    try:
        record = writer.append(
            {"v": 1, "at": utc_now(), "type": "feedback", "body": body}
        )
    except RecordHeld:
        return Problem(503, "store_not_recording")
    except (StoreError, sqlite3.Error):  # includes RecordRefused and a busy lock
        return Problem(503, "busy")
    return {"record": record}
