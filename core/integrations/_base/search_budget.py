"""How many searches an integration's SIEM has been sent in the last hour, per process.

Every check that searches a SIEM (Kibana and Wazuh Sees on the elastic-siem
budget; Quiet later) calls ``spend`` before each search, with the limit
its descriptor's ``max_searches_per_hour`` resolves to. Listing rules and
metadata calls (``_find``, ``_field_caps``) are not searches and do not spend.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional

WINDOW_SECONDS = 3600.0

# Integration id -> monotonic times of the searches in the rolling window.
spent: Dict[str, Deque[float]] = {}
_lock = threading.Lock()


def spend(integration_id: str, per_hour: int, now: Optional[float] = None) -> bool:
    """Count one search against the hour, or False when the budget is spent."""
    now = time.monotonic() if now is None else now
    with _lock:
        window = spent.setdefault(integration_id, deque())
        while window and window[0] <= now - WINDOW_SECONDS:
            window.popleft()
        if len(window) >= per_hour:
            return False
        window.append(now)
        return True
