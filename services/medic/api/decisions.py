"""Decision-log reads: `GET /v1/decisions` and `GET /v1/decisions/{seq}`.

`encode_cursor` / `decode_cursor` bind a caller's filter string to an
uninterpreted position and return a `Cursor` token. They do not sign, and they
do not know what the filters mean. The incident list uses the same pair.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from services.medic.store import StoreError, open_reader

_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "decision-record.schema.json"
    ).read_text()
)
_TYPES = set(_SCHEMA["properties"]["type"]["enum"])
_INCIDENT = re.compile(_SCHEMA["$defs"]["incident_id"]["pattern"])

# OpenAPI: seq is an integer >= 0; limit defaults to 50 and stops at 100.
_SEQ = re.compile(r"0|[1-9][0-9]{0,18}")
# The path seq has no upper bound: a well-formed seq too big for SQLite is absent (404).
_SEQ_PATH = re.compile(r"0|[1-9][0-9]*")
_LIMIT = re.compile(r"[1-9][0-9]?|100")
_CURSOR = re.compile(r"[A-Za-z0-9_-]{1,256}")
# Error.field. A name that doesn't fit is omitted rather than echoed.
_FIELD = re.compile(r"[A-Za-z0-9_/.-]{1,64}")
_DEFAULT_LIMIT = 50
_SQL_MAX = 2**63 - 1
_REPEATS = {"incident_id": 1, "type": 8, "from_seq": 1, "cursor": 1, "limit": 1}
# One statement, placeholders only. Unused type slots are NULL, which IN
# never matches; a zero count skips the type test. Eight slots is the
# contract's maxItems for `type`.
_TYPE_SLOTS = _REPEATS["type"]
_PAGE = (
    "SELECT seq, record FROM records WHERE seq >= ?"
    " AND (? IS NULL OR json_extract(record, '$.body.incident_id') = ?)"
    " AND (? = 0 OR json_extract(record, '$.type') IN (?,?,?,?,?,?,?,?))"
    " ORDER BY seq LIMIT ?"
)


@dataclass(frozen=True)
class Problem:
    status: int
    code: str
    field: str | None = None


class _Bad(Exception):
    def __init__(self, code: str, field: str | None = None) -> None:
        self.problem = Problem(400, code, field)


@dataclass(frozen=True)
class _Query:
    incident_id: str | None
    types: tuple[str, ...]
    start: int
    limit: int


def encode_cursor(filters: str, position: str) -> str:
    """A token carrying `filters` and `position`. The position is not interpreted.

    Raises ValueError when the token would be longer than a `Cursor` (256)."""
    raw_filters = filters.encode()
    try:
        blob = len(raw_filters).to_bytes(2, "big") + raw_filters + position.encode()
    except OverflowError:
        raise ValueError("cursor does not fit the contract") from None
    token = base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")
    if _CURSOR.fullmatch(token) is None:
        raise ValueError("cursor does not fit the contract")
    return token


def decode_cursor(token: str) -> tuple[str, str] | None:
    """`(filters, position)`, or None when `token` is not one this pair encoded."""
    if not isinstance(token, str) or _CURSOR.fullmatch(token) is None:
        return None
    try:
        blob = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if len(blob) < 2:
            return None
        size = int.from_bytes(blob[:2], "big")
        if len(blob) < 2 + size:
            return None
        filters = blob[2 : 2 + size].decode()
        position = blob[2 + size :].decode()
    except (ValueError, UnicodeDecodeError):
        return None
    try:
        if encode_cursor(filters, position) != token:
            return None
    except ValueError:
        return None
    return filters, position


def _field(name: str) -> str | None:
    return name if _FIELD.fullmatch(name) else None


def _filters(incident_id: str | None, types: tuple[str, ...]) -> str:
    """Short on purpose: the token has to stay inside `Cursor`'s 256 characters,
    and a full incident id plus eight types is a legal page."""
    parts = []
    if incident_id is not None:
        parts.append("i=" + incident_id)
    if types:
        parts.append("t=" + ",".join(types))
    return "&".join(parts)


def _parse_list(query: str) -> _Query:
    counts: dict[str, int] = {}
    incident_id: str | None = None
    types: list[str] = []
    from_seq: int | None = None
    cursor: str | None = None
    limit = _DEFAULT_LIMIT
    for name, value in parse_qsl(query, keep_blank_values=True):
        cap = _REPEATS.get(name)
        if cap is None:
            raise _Bad("invalid_parameter", _field(name))
        counts[name] = counts.get(name, 0) + 1
        if counts[name] > cap:
            raise _Bad("invalid_parameter", name)
        if name == "incident_id":
            if _INCIDENT.fullmatch(value) is None:
                raise _Bad("invalid_parameter", "incident_id")
            incident_id = value
        elif name == "type":
            if value not in _TYPES:
                raise _Bad("invalid_parameter", "type")
            types.append(value)
        elif name == "from_seq":
            # Same bound as the path seq: a start past SQLite's range is an empty page.
            if _SEQ_PATH.fullmatch(value) is None:
                raise _Bad("invalid_parameter", "from_seq")
            from_seq = int(value)
        elif name == "cursor":
            if _CURSOR.fullmatch(value) is None:
                raise _Bad("invalid_cursor", "cursor")
            cursor = value
        elif _LIMIT.fullmatch(value) is None:
            raise _Bad("invalid_parameter", "limit")
        else:
            limit = int(value)
    if from_seq is not None and cursor is not None:
        raise _Bad("invalid_parameter", "from_seq")
    chosen = tuple(sorted(set(types)))
    start = 0 if from_seq is None else from_seq
    if cursor is not None:
        decoded = decode_cursor(cursor)
        if decoded is None:
            raise _Bad("invalid_cursor", "cursor")
        bound, position = decoded
        if bound != _filters(incident_id, chosen) or _SEQ.fullmatch(position) is None:
            raise _Bad("invalid_cursor", "cursor")
        start = int(position)
    return _Query(incident_id, chosen, start, limit)


def _reject_query(query: str) -> None:
    for name, _value in parse_qsl(query, keep_blank_values=True):
        raise _Bad("invalid_parameter", _field(name))


def _with_store(data_dir: Path | None, read):
    """One reader per call, closed before this returns. A missing or unreadable
    store is `busy` (the caller learns nothing about the path)."""
    if data_dir is None:
        return Problem(503, "busy")
    try:
        with open_reader(data_dir) as conn:
            return read(conn)
    except (StoreError, sqlite3.Error, json.JSONDecodeError):
        return Problem(503, "busy")


def _page(conn: sqlite3.Connection, query: _Query) -> dict[str, Any]:
    if query.start > _SQL_MAX:
        rows: list[tuple[Any, str]] = []
    else:
        # `type` is the record's top-level field. `incident_id` is inside `body`;
        # a record with no body.incident_id does not match that filter.
        slots = (list(query.types) + [None] * _TYPE_SLOTS)[:_TYPE_SLOTS]
        rows = conn.execute(
            _PAGE,
            (
                query.start,
                query.incident_id,
                query.incident_id,
                len(query.types),
                *slots,
                query.limit + 1,
            ),
        ).fetchall()
    (oldest,) = conn.execute("SELECT MIN(seq) FROM records").fetchone()
    extra = len(rows) > query.limit
    kept = rows[: query.limit]
    # The next page starts strictly after this page's last seq, so an append
    # during the scan can neither repeat a row nor skip one already passed.
    nxt = None
    if extra:
        nxt = encode_cursor(
            _filters(query.incident_id, query.types), str(kept[-1][0] + 1)
        )
    return {
        "items": [json.loads(record) for _, record in kept],
        "next_cursor": nxt,
        "oldest_seq": oldest,
    }


def list_page(data_dir: Path | None, query: str) -> dict | Problem:
    try:
        parsed = _parse_list(query)
    except _Bad as bad:
        return bad.problem
    return _with_store(data_dir, lambda conn: _page(conn, parsed))


def get_decision(data_dir: Path | None, seq_text: str, query: str) -> dict | Problem:
    try:
        _reject_query(query)
        if _SEQ_PATH.fullmatch(seq_text) is None:
            raise _Bad("invalid_parameter", "seq")
    except _Bad as bad:
        return bad.problem
    if len(seq_text) > 19:  # at least 10**19, past _SQL_MAX
        return Problem(404, "not_found")
    seq = int(seq_text)

    def read(conn: sqlite3.Connection) -> dict | Problem:
        if seq > _SQL_MAX:
            return Problem(404, "not_found")
        row = conn.execute(
            "SELECT record FROM records WHERE seq = ?", (seq,)
        ).fetchone()
        if row is None:
            return Problem(404, "not_found")
        return json.loads(row[0])

    return _with_store(data_dir, read)
