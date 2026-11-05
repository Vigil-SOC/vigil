"""The live run's baseline, for the daemon's triage of new alerts.

Read per finding, so it is cached briefly. A failed read means no baseline: triage
then runs as it would without a cold start.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

from core.coldstart.baseline import Baseline
from core.coldstart.store import live_baseline_dict

logger = logging.getLogger(__name__)

CACHE_SECONDS = 30.0

_cached: Optional[Tuple[float, Optional[Baseline]]] = None


def live_baseline() -> Optional[Baseline]:
    global _cached
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < CACHE_SECONDS:
        return _cached[1]
    baseline: Optional[Baseline] = None
    try:
        stored = live_baseline_dict()
        baseline = Baseline.from_dict(stored) if stored else None
    except Exception as e:  # noqa: BLE001
        logger.warning("Live baseline read failed: %s", type(e).__name__)
    _cached = (now, baseline)
    return baseline


def forget() -> None:
    global _cached
    _cached = None
