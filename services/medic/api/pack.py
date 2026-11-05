"""`GET /v1/pack` and the three pack writes under `/v1/pack/` (F6, #2026).

The listener's threads only check and queue. The main loop is the one writer: it
takes each request at a cycle boundary, runs the lifecycle operation, records the
`pack_event`s and swaps the engine (app/pack_admin.py). An import is held to the
admin's expected sha256 and vetted here first (`loader.vet_pack`), so a bad pack is
refused on this thread and never reaches the loop. A request nobody picked up within `timeout` is abandoned: it is
answered `busy` and the loop skips it, so it can't apply later.
"""

from __future__ import annotations

import logging
import queue
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from services.medic.api.decisions import (
    _SCHEMA,
    Problem,
    _Bad,
    _field,
    _reject_query,
    _with_store,
)
from services.medic.contracts.pack_check import MAX_PACK_BYTES, check_digest
from services.medic.packs import lifecycle
from services.medic.packs.loader import vet_pack

log = logging.getLogger("services.medic")

MAX_BODY = 4096  # the body-less operations
_ADMIN = re.compile(_SCHEMA["$defs"]["id"]["pattern"])
OPS = ("import", "revert", "switch-to-bundled")
_SHA256 = re.compile(r"[0-9A-Fa-f]{64}")
# Refusals about this install's state, not about the pack file (409 pack_conflict).
CONFLICTS = frozenset(
    {
        lifecycle.WITHDRAWN,
        "P-LINEAGE",  # another (id, channel) than the active pack, without override
        "P-DOWNGRADE",  # an older version on the same line, override or not
        lifecycle.NO_TARGET,
        lifecycle.NO_ACTIVE,
        lifecycle.BUNDLED_ACTIVE,
    }
)
SUPPRESSION_DAYS = 7
SUPPRESSION_MAX = 200  # PackState.suppression_7d maxItems

_SUPPRESSED = (
    "SELECT json_extract(record, '$.type'), json_extract(record, '$.body.incident_id'),"
    " json_extract(record, '$.body.rule.id'), json_extract(record, '$.body.suppressed_by'),"
    " json_extract(record, '$.at') FROM records"
    " WHERE json_extract(record, '$.type') = 'incident_opened'"
    " OR (json_extract(record, '$.type') = 'incident_updated'"
    " AND json_extract(record, '$.body.change') = 'suppressed')"
    " ORDER BY seq"  # the latest parent a child names wins
)


class _Request:
    """One admin operation on its way to the loop. `state` moves pending →
    running (the loop took it) or pending → abandoned (the caller gave up)."""

    def __init__(
        self, op: str, by: str, data: bytes, override: bool, timeout: float
    ) -> None:
        self.op, self.by, self.data, self.override = op, by, data, override
        self.deadline = time.monotonic() + timeout  # the loop honours it too
        self.lock = threading.Lock()
        self.state = "pending"
        self.done = threading.Event()
        self.result: dict[str, Any] | Problem = Problem(500, "internal")


class PackBridge:
    """The hand-off between the listener's threads and the main loop. With no
    `vigil` there is no pack lifecycle (dev mode) and every call is refused."""

    def __init__(self, vigil: str | None = None) -> None:
        self.vigil = vigil
        self._queue: queue.SimpleQueue[_Request] = queue.SimpleQueue()

    @property
    def available(self) -> bool:
        return self.vigil is not None

    def submit(
        self, op: str, *, by: str, data: bytes = b"", override: bool = False,
        timeout: float,
    ) -> dict[str, Any] | Problem:  # fmt: skip
        """The loop's answer, or `busy` if it didn't take the request in time."""
        req = _Request(op, by, data, override, timeout)
        self._queue.put(req)
        if not req.done.wait(timeout):
            with req.lock:
                if req.state != "running":
                    req.state = "abandoned"
                    return Problem(503, "busy")
            req.done.wait()  # the loop already took it: it is a moment from done
        return req.result

    def claim(self) -> list[_Request]:
        """Called by the loop: every request still wanted, now marked running."""
        out = []
        while True:
            try:
                req = self._queue.get_nowait()
            except queue.Empty:
                return out
            with req.lock:
                # Past its deadline the caller has been, or is about to be, told
                # `busy` (a stalled process wakes both sides at once): never run it.
                if req.state == "pending" and time.monotonic() < req.deadline:
                    req.state = "running"
                    out.append(req)
                else:
                    req.state = "abandoned"


def refusal(errors: list[tuple[str, str]]) -> Problem:
    """The Problem for a lifecycle or verifier refusal: its first code, closed."""
    code = errors[0][0]
    if code in CONFLICTS:
        return Problem(409, "pack_conflict", pack_code=code)
    return Problem(422, "pack_refused", pack_code=code)


def post_pack(
    op: str,
    bridge: PackBridge | None,
    wall: Callable[[], float],
    query: str,
    *,
    content_type: str | None,
    length: str | None,
    admins: list[str],
    read_body,
    timeout: float,
) -> dict | Problem:
    """`read_body(n)` is called only after every cheaper check has passed."""
    override, sha256 = False, None
    try:
        if op == "import":
            override, sha256 = _import_query(query)
        else:
            _reject_query(query)
    except _Bad as bad:
        return bad.problem
    if op == "import":
        media = (content_type or "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            return Problem(415, "unsupported_media_type")
    if length is None or not length.isascii() or not length.isdigit():
        return Problem(400, "invalid_parameter", "Content-Length")
    if int(length) > (MAX_PACK_BYTES if op == "import" else MAX_BODY):
        return Problem(413, "payload_too_large")
    if len(admins) != 1 or _ADMIN.fullmatch(admins[0]) is None:
        return Problem(422, "missing_admin_identity", "X-Medic-Admin")
    if bridge is None or not bridge.available:
        return Problem(409, "pack_unavailable")
    data = read_body(int(length))
    if op != "import":
        if data:
            return Problem(422, "invalid_body")
    else:
        if digest := check_digest(data, sha256):  # logged, never a pack_event
            log.warning("pack import by %s refused: %s", admins[0], digest[1])
            return refusal([digest])
        now = datetime.fromtimestamp(wall(), UTC)
        try:
            errors, _ = vet_pack(
                data, vigil_version=bridge.vigil, now=now, lineage=None
            )
        except Exception as exc:  # noqa: BLE001 (a hostile file never ends the thread)
            log.error("pack import vetting failed: %s", type(exc).__name__)
            return Problem(500, "internal")
        if errors:
            log.warning(
                "pack import by %s refused: %s",
                admins[0],
                sorted({c for c, _ in errors}),
            )
            return refusal(errors)
    return bridge.submit(
        op, by=admins[0], data=data if op == "import" else b"", override=override,
        timeout=timeout,
    )  # fmt: skip


def _import_query(query: str) -> tuple[bool, str]:
    """(override, sha256 in lowercase). `sha256` is required, each name at most once."""
    seen: dict[str, str] = {}
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name not in ("sha256", "override") or name in seen:
            raise _Bad("invalid_parameter", _field(name))
        seen[name] = value
    if seen.get("override", "false") not in ("true", "false"):
        raise _Bad("invalid_parameter", "override")
    if _SHA256.fullmatch(seen.get("sha256", "")) is None:
        raise _Bad("invalid_parameter", "sha256")
    return seen.get("override") == "true", seen["sha256"].lower()


def suppression_7d(data_dir: Path, now: datetime) -> list[dict[str, Any]]:
    """Incidents suppressed per suppressing rule over the last 7 days: each child
    counted once, under the rule of the parent that suppressed it (the last one
    named). Zero counts are omitted. A store that can't be read gives []."""
    cutoff = (now - timedelta(days=SUPPRESSION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rules: dict[str, str | None] = {}
        children: dict[str, str] = {}  # child -> its latest parent in the window
        for kind, iid, rule, parent, at in conn.execute(_SUPPRESSED):
            if kind == "incident_opened":
                rules[iid] = rule
            elif at >= cutoff:
                children[iid] = parent
        counts: dict[str, int] = {}
        for parent in children.values():
            if rule := rules.get(parent):
                counts[rule] = counts.get(rule, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [{"rule": r, "suppressed": n} for r, n in ranked[:SUPPRESSION_MAX]]

    found = _with_store(data_dir, read)
    return [] if isinstance(found, Problem) else found
