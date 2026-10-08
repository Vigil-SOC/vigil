"""The heartbeat file and the `check` that reads it (C5 §5.1).

The main loop writes `<data root>/run/heartbeat` after every completed cycle.
`check` reads that one file and nothing else: no socket, no store, no API. It is
the same command on Compose, Helm and host-native, copied from `arq --check`.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

# C5 time budget (ENG #2): a beat at least every 30 s (S4 beats every 15 s cycle), stale after 4 missed beats,
# 300 s grace for start-up, and the watchdog later than the probe budget so on
# Helm the kubelet acts first.
BEAT_INTERVAL_S = 30
STALE_AFTER_S = 120
STARTUP_GRACE_S = 300
WATCHDOG_AFTER_S = 180

# A beat this far ahead of now is a clock fault, not a fresh beat; without the
# limit a clock that jumped ahead would read as healthy forever.
FUTURE_SLACK_S = 60

FORMAT_VERSION = 1


def heartbeat_path(data_dir: Path) -> Path:
    return data_dir / "run" / "heartbeat"


def write_heartbeat(
    data_dir: Path,
    *,
    cycle: int,
    now: float,
    started_at: float,
    pid: int,
    state: str = "running",
) -> None:
    """Write atomically (temp file, then rename), so `check` never reads half a file."""
    path = heartbeat_path(data_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir leaves an existing dir's mode alone; run/ is Medic's own, so tighten
    # it. The data root's own checks (Helm's 2770 allowed) are the store's (S2).
    path.parent.chmod(0o700)
    record = {
        "v": FORMAT_VERSION,
        "cycle": cycle,
        "ts": now,
        "started_at": started_at,
        "pid": pid,
        "state": state,
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".heartbeat.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _parse(path: Path) -> dict:
    record = json.loads(path.read_text())
    out = {
        "ts": float(record["ts"]),
        "started_at": float(record["started_at"]),
        "cycle": int(record["cycle"]),
        "pid": int(record.get("pid", 0)),
        "state": str(record.get("state", "running")),
    }
    if not (math.isfinite(out["ts"]) and math.isfinite(out["started_at"])):
        raise ValueError("non-finite time")
    return out


def read_heartbeat(data_dir: Path) -> dict | None:
    """The last beat (ts, started_at, cycle, pid, state), or None if missing or bad."""
    try:
        return _parse(heartbeat_path(data_dir))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def mark_stalled(data_dir: Path) -> None:
    """C5 §5.1 step 2: the watchdog says why the beats stopped, for the next
    start's `gap` record. `ts` stays the last good beat."""
    beat = read_heartbeat(data_dir)
    if beat is None:
        return
    write_heartbeat(
        data_dir,
        cycle=beat["cycle"],
        now=beat["ts"],
        started_at=beat["started_at"],
        pid=beat["pid"],
        state="stalled",
    )


def check_heartbeat(data_dir: Path, *, now: float) -> tuple[bool, str]:
    """Return (healthy, reason). Healthy only if the beat is fresh."""
    path = heartbeat_path(data_dir)
    try:
        beat = _parse(path)
    except FileNotFoundError:
        return False, f"no heartbeat at {path}"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return False, f"heartbeat unreadable at {path}: {type(exc).__name__}"
    ts, started_at, cycle, state = (
        beat["ts"],
        beat["started_at"],
        beat["cycle"],
        beat["state"],
    )

    if state in ("stopped", "stalled"):
        return False, f"Medic {state} (cycle {cycle})"
    age = now - ts
    if age < -FUTURE_SLACK_S:
        return False, f"heartbeat is {-age:.0f} s in the future (clock fault?)"
    limit = STARTUP_GRACE_S if now - started_at < STARTUP_GRACE_S else STALE_AFTER_S
    if age > limit:
        return (
            False,
            f"heartbeat stale: {age:.0f} s old (limit {limit} s, cycle {cycle})",
        )
    return True, f"ok: heartbeat {max(age, 0):.0f} s old (cycle {cycle})"
