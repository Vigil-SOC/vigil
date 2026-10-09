"""The body of `GET /v1/status` (X2 `Status`), built once per cycle by the main loop.

Medic reports only states it can know about itself (C5 §5.4): `down`, `off` and
`unknown` are the backend's. Everything here is typed: enums, integers,
timestamps and ids, never text from Vigil (K1 G2).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from services.medic.api.history import (
    CRASH_LOOP_RESTARTS,
    CRASH_LOOP_WINDOW_S,
    DAY_S,
    History,
)
from services.medic.app.gap import iso
from services.medic.app.heartbeat import STARTUP_GRACE_S
from services.medic.store import StoreError, open_reader
from services.medic.store.writer import StoreUsage

API_VERSION = "1.0"

# C4 §6.6 names for S2's usage() states. Over the cap the store is writing into
# its 64 MB reserve (G3 enforces the cap; S2 only reports it).
_STORE_STATES = {"ok": "ok", "near_cap": "near_cap", "over_cap": "on_reserve"}
_NOT_RECORDING = {"recording_stopped", "migration_blocked"}


def medic_state(
    *,
    uptime_s: int,
    restarts_10m: int,
    sensors: dict[str, int],
    store_states: list[str],
    decisions_lost: int,
) -> str:
    """C5 §5.4, worst wins. Inside the start-up grace, sensors that can't see yet
    are expected (Vigil may still be starting), so `starting` outranks them; a
    store that isn't recording and a crash loop are never expected."""
    if restarts_10m > CRASH_LOOP_RESTARTS:
        return "crash_looping"
    if _NOT_RECORDING & set(store_states):
        return "not_recording"
    if uptime_s < STARTUP_GRACE_S:
        return "starting"
    if sensors["cant_see"] and not sensors["reporting"]:
        return "blind"
    if sensors["cant_see"] or store_states != ["ok"] or decisions_lost:
        return "degraded"
    return "running"


def read_chain(data_dir: Path) -> dict[str, Any]:
    """The chain head and the oldest/newest record times, through a reader (C4 §6.7)."""
    empty = {"head": None, "oldest_at": None, "newest_at": None}
    try:
        with open_reader(data_dir) as conn:
            first = conn.execute(
                "SELECT record FROM records ORDER BY seq LIMIT 1"
            ).fetchone()
            last = conn.execute(
                "SELECT seq, hash, record FROM records ORDER BY seq DESC LIMIT 1"
            ).fetchone()
    except (StoreError, sqlite3.Error):
        return empty
    if first is None or last is None:
        return empty
    return {
        "head": {"seq": last[0], "hash": last[1]},
        "oldest_at": json.loads(first[0])["at"],
        "newest_at": json.loads(last[2])["at"],
    }


def _store(usage: StoreUsage, decisions_lost: int, chain: dict[str, Any]) -> dict:
    over = max(0, usage.used_bytes - usage.cap_bytes)
    return {
        "states": [_STORE_STATES.get(usage.state, "ok")],
        "backend": "sqlite",
        "bytes_used": usage.used_bytes,
        "cap_bytes": usage.cap_bytes,
        "reserve_left_bytes": max(0, usage.reserve_bytes - over),
        "disk_free_bytes": usage.free_bytes,
        "oldest_record_at": chain["oldest_at"],
        "newest_record_at": chain["newest_at"],
        "decisions_lost": decisions_lost,
    }


def _sensors(scheduler: dict[str, Any]) -> dict[str, int]:
    off, cant_see = len(scheduler["off"]), len(scheduler["blind"])
    return {
        "reporting": max(0, scheduler["sensors"] - off - cant_see),
        "cant_see": cant_see,
        "off": off,
    }


def build_status(
    *,
    instance_id: str,
    now: float,
    started_at: float,
    heartbeat_at: float,
    cycle: int,
    history: History,
    scheduler: dict[str, Any],
    usage: StoreUsage,
    decisions_lost: int,
    chain: dict[str, Any],
) -> dict[str, Any]:
    """`scheduler` is `Scheduler.status()`; `usage` is the writer's `usage()`."""
    uptime_s = max(0, int(now - started_at))
    sensors = _sensors(scheduler)
    store = _store(usage, decisions_lost, chain)
    return {
        "api_version": API_VERSION,
        "instance_id": instance_id,
        "state": medic_state(
            uptime_s=uptime_s,
            restarts_10m=history.restarts_since(now - CRASH_LOOP_WINDOW_S),
            sensors=sensors,
            store_states=store["states"],
            decisions_lost=decisions_lost,
        ),
        "now": iso(now),
        "heartbeat_at": iso(heartbeat_at),
        "cycle": cycle,
        "started_at": iso(started_at),
        "uptime_s": uptime_s,
        "restarts_24h": history.restarts_since(now - DAY_S),
        "last_exit": history.last_exit,
        # Unsigned dev rules until F6 loads signed packs (UX-9).
        "dev_mode": True,
        "store": store,
        "sensors": sensors,
        "pack": None,
        "chain_head": chain["head"],
    }


def serve_view(snapshot: dict[str, Any], *, now: float, started_at: float) -> dict:
    """The snapshot as served: `now` (so the backend can spot skew) and `uptime_s`
    at the moment of the request; everything else as the last cycle saw it."""
    return {**snapshot, "now": iso(now), "uptime_s": max(0, int(now - started_at))}
