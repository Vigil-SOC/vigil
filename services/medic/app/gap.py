"""The `gap` record (S2-5 (a); C5 §5.1 step 2, C4 §6.8): a time Medic wasn't watching.

Read at start-up, before the new heartbeat replaces the old one. The last
heartbeat is the last sign of life: `stalled` if the watchdog marked it, `off`
for anything else (a clean stop, a kill, a crash, the host going down). With no
readable heartbeat, the newest record stands in for it. A first start (no
heartbeat, empty store) has nothing to record.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.medic.app.heartbeat import read_heartbeat
from services.medic.store import DecisionWriter, StoreError, open_reader

log = logging.getLogger("services.medic")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_record(data_dir: Path) -> dict[str, Any] | None:
    try:
        with open_reader(data_dir) as conn:
            row = conn.execute(
                "SELECT record FROM records ORDER BY seq DESC LIMIT 1"
            ).fetchone()
    except (StoreError, sqlite3.Error):
        return None  # no store yet; open_writer reports a broken one
    return json.loads(row[0]) if row else None


def pending_gap(data_dir: Path, *, now: float) -> dict[str, Any] | None:
    """The gap body to write at this start-up, or None."""
    last = _last_record(data_dir)
    beat = read_heartbeat(data_dir)
    if beat is not None:
        since = beat["ts"]
        reason = "stalled" if beat["state"] == "stalled" else "off"
    elif last is not None:
        since = datetime.fromisoformat(last["at"]).timestamp()
        reason = "off"
    else:
        return None
    if since > now:
        # The clock stepped back. A gap that ends before it starts would fail
        # verify() (E-GAP), so it is recorded as zero length at the restart.
        log.warning("Last sign of life is %.0f s in the future", since - now)
        since = now
    gap = {"from": iso(since), "to": iso(now), "reason": reason}
    if (
        last is not None
        and last["type"] == "gap"
        and last["body"]["from"] == gap["from"]
    ):
        return None  # already written; Medic died before its new heartbeat
    return gap


def write_gap(writer: DecisionWriter, gap: dict[str, Any] | None) -> None:
    if gap is None:
        return
    stored = writer.append({"v": 1, "at": gap["to"], "type": "gap", "body": gap})
    log.warning(
        "Medic was not watching from %s to %s (%s); recorded as seq %d",
        gap["from"],
        gap["to"],
        gap["reason"],
        stored["seq"],
    )
