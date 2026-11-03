"""Log a repeating LLM-path outage once on entry, then a count, then recovery.

Reminder and recovery keep the caller's message as their template, plus a fixed
suffix, so each outage matches on its own constant string. State is module-level,
like ``core/integrations/_base/config_gap.py``: the callers are per-finding or
per-refresh-tick, so a repeat must not log per call.
"""

from __future__ import annotations

import logging
from typing import Dict, Set

# Failures between "still failing" reminders.
REMINDER_EVERY = 100

# outage key -> failures since the first ERROR
_failures: Dict[str, int] = {}
# keys already logged healthy in this process
_healthy: Set[str] = set()


def report_outage(
    logger: logging.Logger, key: str, message: str, *args: object
) -> None:
    """ERROR on the first failure of ``key``, then every REMINDER_EVERY failures."""
    _healthy.discard(key)  # recovery, not a second healthy line, follows
    count = _failures.get(key, 0) + 1
    _failures[key] = count
    if count == 1:
        logger.error(message, *args)
    elif count % REMINDER_EVERY == 0:
        # Concatenate; do not pre-format. The caller's literal stays the template.
        logger.error(message + " (still failing, %d failures)", *args, count)


def report_recovered(
    logger: logging.Logger, key: str, message: str, *args: object
) -> bool:
    """INFO once when ``key`` works again; False if it was not failing.

    The first success of a key in this process also logs ``message`` once, so
    callers pass wording that reads as healthy."""
    count = _failures.pop(key, None)
    first = key not in _healthy
    _healthy.add(key)
    if count is None:
        if first:
            logger.info(message, *args)
        return False
    logger.info(message + "; recovered after %d failures", *args, count)
    return True
