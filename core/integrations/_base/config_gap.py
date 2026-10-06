"""Log an unfinished integration config once per condition, not once per poll.

Ingestion classes are rebuilt per federation tick, so the state is module-level.
Only field names are logged, never values.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable

# Skipped polls between "still incomplete" reminders.
REMINDER_EVERY = 60

# source name -> polls skipped since the first warning
_skipped: Dict[str, int] = {}


def report_config_gap(
    logger: logging.Logger, source: str, missing: Iterable[str]
) -> None:
    """Warn on the first incomplete poll, then only every REMINDER_EVERY skips."""
    if source not in _skipped:
        _skipped[source] = 1
        logger.warning(
            "%s configuration incomplete (missing: %s); skipping polls until it is "
            "completed",
            source,
            ", ".join(missing),
        )
        return
    _skipped[source] += 1
    if _skipped[source] % REMINDER_EVERY == 0:
        logger.warning(
            "%s configuration still incomplete (missing: %s); %d polls skipped",
            source,
            ", ".join(missing),
            _skipped[source],
        )


def report_config_complete(logger: logging.Logger, source: str) -> None:
    """Log one INFO line when a previously incomplete config is now usable."""
    skipped = _skipped.pop(source, None)
    if skipped is not None:
        logger.info(
            "%s configuration complete; polling resumed after %d skipped polls",
            source,
            skipped,
        )
