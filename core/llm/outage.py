"""Log a repeating LLM-path outage once on entry, then a count, then recovery.

State is module-level, like ``core/integrations/_base/config_gap.py``: the callers
are per-finding or per-refresh-tick, so a repeat must not log per call.
"""

from __future__ import annotations

import logging
from typing import Dict

# Failures between "still failing" reminders.
REMINDER_EVERY = 100

# outage key -> failures since the first ERROR
_failures: Dict[str, int] = {}


def report_outage(
    logger: logging.Logger, key: str, message: str, *args: object
) -> None:
    """ERROR on the first failure of ``key``, then every REMINDER_EVERY failures."""
    count = _failures.get(key, 0) + 1
    _failures[key] = count
    if count == 1:
        logger.error(message, *args)
    elif count % REMINDER_EVERY == 0:
        logger.error(
            "%s (still failing, %d failures)",
            message % args if args else message,
            count,
        )


def report_recovered(logger: logging.Logger, key: str, message: str) -> bool:
    """INFO once when ``key`` works again. False if it was not failing."""
    count = _failures.pop(key, None)
    if count is None:
        return False
    logger.info("%s; recovered after %d failures", message, count)
    return True
