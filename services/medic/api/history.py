"""Restarts and the last exit reason, for `/v1/status` (C5 §5.4).

One small file, `<data dir>/run/starts.json`, written at each start and at a
clean stop. A run that ends without saying goodbye (watchdog, SIGKILL, OOM, the
host-native loop giving up) leaves only its heartbeat, so the next start reads
the reason from that. Never a reason for Medic to stop: every error here is
logged and skipped.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.medic.app.gap import iso

log = logging.getLogger("services.medic")

DAY_S = 86_400
# C5 §5.4: "restarted more than 5 times in 10 minutes".
CRASH_LOOP_RESTARTS = 5
CRASH_LOOP_WINDOW_S = 600
MAX_KEPT = 10_000  # a day of restarts every 9 s; beyond that it's crash_looping anyway
FORMAT_VERSION = 1

# The last heartbeat's state, when the run that wrote it recorded no exit.
_BEAT_REASON = {
    "stalled": "watchdog_stall",
    "crash-looping": "crash",
    "stopped": "clean",
}


@dataclass(frozen=True)
class History:
    restarts: tuple[float, ...]  # start times that followed an earlier run, ≤ 24 h
    last_exit: dict[str, str] | None  # X2 `last_exit`: {reason, at}

    def restarts_since(self, since: float) -> int:
        return sum(1 for t in self.restarts if t >= since)


def history_path(data_dir: Path) -> Path:
    return data_dir / "run" / "starts.json"


def _load(data_dir: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(history_path(data_dir).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning(
            "Start history unreadable, starting it fresh: %s", type(exc).__name__
        )
        return None
    return doc if isinstance(doc, dict) and doc.get("v") == FORMAT_VERSION else None


def _save(data_dir: Path, doc: dict[str, Any]) -> None:
    path = history_path(data_dir)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".starts.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(doc, f, allow_nan=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        log.warning("Start history not written: %s", type(exc).__name__)


def _floats(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    return [float(t) for t in value if isinstance(t, int | float)]


def _exit(value: Any) -> dict[str, str] | None:
    if isinstance(value, dict) and set(value) == {"reason", "at"}:
        return {"reason": str(value["reason"]), "at": str(value["at"])}
    return None


def record_start(
    data_dir: Path, *, now: float, previous_beat: dict[str, Any] | None
) -> History:
    """Call once per start, with the heartbeat as it was before this start's beat."""
    doc = _load(data_dir)
    ran_before = doc is not None or previous_beat is not None
    last_exit = _exit(doc.get("exit")) if doc else None
    if last_exit is None and ran_before:
        state = previous_beat["state"] if previous_beat else None
        ts = previous_beat["ts"] if previous_beat else 0
        last_exit = {
            "reason": _BEAT_REASON.get(state, "unknown"),
            # The host-native loop's crash-looping beat is dated 0 on purpose.
            "at": iso(ts if ts > 0 else now),
        }
    kept = [t for t in _floats((doc or {}).get("restarts")) if now - t < DAY_S]
    if ran_before:
        kept.append(now)
    kept = kept[-MAX_KEPT:]
    _save(
        data_dir,
        {"v": FORMAT_VERSION, "restarts": kept, "last_exit": last_exit, "exit": None},
    )
    return History(restarts=tuple(kept), last_exit=last_exit)


def record_exit(data_dir: Path, *, now: float, code: int) -> None:
    """A run that ends through `run`'s own return: 0 is a stop, anything else a crash."""
    doc = _load(data_dir) or {"v": FORMAT_VERSION, "restarts": [], "last_exit": None}
    doc["exit"] = {"reason": "clean" if code == 0 else "crash", "at": iso(now)}
    _save(data_dir, doc)
