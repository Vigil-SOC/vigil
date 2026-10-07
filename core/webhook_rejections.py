"""Log and count inbound webhook rejections (401/503 auth or gate failures).

Shared by the API receivers and the daemon ingest webhook so a rotated or
mistyped secret leaves a trace instead of silently dropping alerts.

Everything is keyed on the fixed ``(endpoint, reason)`` pair supplied by the
call sites, never on client input, so memory stays bounded however hard the
endpoint is hammered. Callers must never pass secrets, tokens, signatures or
bodies in ``detail``.
"""

import logging
import threading
import time
from collections import defaultdict
from typing import Dict, Optional, Tuple

from core.telemetry import get_meter

logger = logging.getLogger(__name__)

BAD_SIGNATURE = "bad_signature"
MISSING_SIGNATURE = "missing_signature"
BAD_TOKEN = "bad_token"
DISABLED = "disabled"
NO_SECRET = "no_secret"
SECRET_LOOKUP_FAILED = "secret_lookup_failed"

LOG_WINDOW_SECONDS = 60.0

_lock = threading.Lock()
_counts: Dict[Tuple[str, str], int] = defaultdict(int)
# (endpoint, reason) -> (monotonic time of last log line, rejections suppressed since)
_log_state: Dict[Tuple[str, str], Tuple[float, int]] = {}
_clock = time.monotonic
_counter = None


def _otel_add(endpoint: str, reason: str) -> None:
    """Bump the OTEL counter; created lazily so it binds to the real meter."""
    global _counter
    try:
        if _counter is None:
            _counter = get_meter("vigil.webhooks").create_counter(
                name="vigil.webhook.rejections.total",
                description="Inbound webhook pushes rejected, by endpoint and reason",
                unit="1",
            )
        _counter.add(1, {"endpoint": endpoint, "reason": reason})
    except Exception as err:  # noqa: BLE001
        logger.debug("OTEL webhook rejection counter unavailable: %s", err)


def record_rejection(
    endpoint: str,
    reason: str,
    client_ip: Optional[str],
    *,
    detail: Optional[str] = None,
    exc: Optional[BaseException] = None,
) -> None:
    """Count one rejection and log it at WARNING, at most once per window per key."""
    key = (endpoint, reason)
    with _lock:
        _counts[key] += 1
        now = _clock()
        last, suppressed = _log_state.get(key, (None, 0))
        if last is not None and now - last < LOG_WINDOW_SECONDS:
            _log_state[key] = (last, suppressed + 1)
            should_log = False
        else:
            _log_state[key] = (now, 0)
            should_log = True
    _otel_add(endpoint, reason)
    if not should_log:
        return
    logger.warning(
        "Webhook rejected: endpoint=%s reason=%s source_ip=%s%s%s",
        endpoint,
        reason,
        client_ip or "unknown",
        f" detail={detail}" if detail else "",
        f" (+{suppressed} similar suppressed in the last window)" if suppressed else "",
        exc_info=exc,
    )


def rejection_counts(prefix: str = "") -> Dict[str, Dict[str, int]]:
    """Counts as ``{endpoint: {reason: n}}`` for endpoints starting with *prefix*."""
    with _lock:
        out: Dict[str, Dict[str, int]] = {}
        for (endpoint, reason), n in sorted(_counts.items()):
            if endpoint.startswith(prefix):
                out.setdefault(endpoint, {})[reason] = n
        return out
