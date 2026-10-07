"""Process-wide bookkeeping for rejected vendor calls (Shodan, VirusTotal, sandboxes).

A revoked key (401/403) or an exhausted quota (429) used to vanish into debug
logs. State is module-level because ``Scheduler._run_sandbox_poll`` builds a
fresh ``SandboxPoller`` on every run.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, Dict, Tuple, Union

logger = logging.getLogger(__name__)

AUTH_STATUSES = (401, 403)
RATE_LIMITED = 429
TRACKED_STATUSES = (*AUTH_STATUSES, RATE_LIMITED)

_WARN_WINDOW_SECONDS = 3600.0
_DEFAULT_COOLDOWN_SECONDS = 60.0

Status = Union[int, str]

_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
_last_warned: Dict[Tuple[str, str], float] = {}
_cooldown_until: Dict[str, float] = {}


def _now() -> float:
    return time.monotonic()


def _hint(status: Status) -> str:
    if status in AUTH_STATUSES:
        return "the API key is likely revoked or expired"
    if status == RATE_LIMITED:
        return "the API key is likely over quota"
    return "the vendor is unreachable or returned an unexpected error"


def _retry_after(raw: Any) -> float:
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return _DEFAULT_COOLDOWN_SECONDS


def record_vendor_error(
    vendor: str, status: Status, retry_after: Any = None, detail: str = ""
) -> None:
    """Count a rejected call; warn at most once per (vendor, status) per hour.

    A 429 also starts the vendor's cool-down (see ``vendor_cooling_down``).
    """
    now = _now()
    key = str(status)
    _counts[vendor][key] += 1
    if status == RATE_LIMITED:
        _cooldown_until[vendor] = now + _retry_after(retry_after)

    last = _last_warned.get((vendor, key))
    if last is not None and now - last < _WARN_WINDOW_SECONDS:
        return
    _last_warned[(vendor, key)] = now
    logger.warning(
        "%s call failed (%s%s): %s",
        vendor,
        status,
        f", {detail}" if detail else "",
        _hint(status),
    )


def note_response(vendor: str, resp: Any) -> bool:
    """Record a 401/403/429 response; return True when it was one."""
    status = resp.status_code
    if status not in TRACKED_STATUSES:
        return False
    retry_after = resp.headers.get("Retry-After") if status == RATE_LIMITED else None
    record_vendor_error(vendor, status, retry_after)
    return True


def vendor_cooling_down(vendor: str) -> bool:
    """True while a prior 429 asks us to leave the vendor alone."""
    return _now() < _cooldown_until.get(vendor, 0.0)


def vendor_error_snapshot() -> Dict[str, Dict[str, Any]]:
    """Per-vendor error counts for the daemon's /status endpoint."""
    return {
        vendor: {"total": sum(by_status.values()), "by_status": dict(by_status)}
        for vendor, by_status in _counts.items()
    }


def reset_vendor_errors() -> None:
    """Clear all state (tests)."""
    _counts.clear()
    _last_warned.clear()
    _cooldown_until.clear()
