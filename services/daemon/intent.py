"""Observe-mode intent report: INTENT.md declared values beside the effective ones.

Run from a shell without a daemon:  python -m services.daemon.intent

The daemon calls :func:`report_intent` once at startup. Lives in ``services``
because the effective values come from ``DaemonConfig.from_env()``, which
``core`` may not import.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

from core.intent import (
    IntentDiff,
    diff_intent,
    effective_values,
    format_rows,
    intent_file,
    read_intent,
)
from core.storage.config_service import get_config_service
from services.daemon.config import DaemonConfig

logger = logging.getLogger(__name__)

FORCE_APPROVAL_PATH = "response.force_manual_approval"


def _overlay_force_manual_approval(
    effective: Dict[str, object], sources: Dict[str, str]
) -> None:
    """The one DB overlay ``from_env()`` does not apply.

    ``ApprovalService`` reads ``approval.force_manual_approval`` itself and the
    env flag forces it on, so the effective value is env OR db. Same
    try/except-and-continue as the orchestrator overlay: no DB, no overlay.
    """
    try:
        row = get_config_service().get_system_config("approval.force_manual_approval")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read approval config from DB: %s", exc)
        return
    if not row or not isinstance(row, dict) or not row.get("enabled"):
        return
    if not effective["respond.force_manual_approval"]:
        effective["respond.force_manual_approval"] = True
        sources[FORCE_APPROVAL_PATH] = "db"


def intent_report(
    config: Optional[DaemonConfig] = None, path: Optional[Path] = None
) -> Optional[List[IntentDiff]]:
    """Rows for every declared key that differs, or ``None`` when unreadable."""
    declared = read_intent(path)
    if declared is None:
        return None
    config = config or DaemonConfig.from_env()
    effective = effective_values(config)
    sources = dict(config.sources)
    _overlay_force_manual_approval(effective, sources)
    return diff_intent(declared, effective, sources)


def report_intent(config: Optional[DaemonConfig] = None) -> None:
    """Log the report. Never raises: a broken manifest must not stop the daemon."""
    try:
        rows = intent_report(config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Intent report failed (non-fatal): %s", exc)
        return
    if rows is None:
        return
    if not rows:
        logger.info("INTENT.md matches effective daemon config (%s)", intent_file())
        return
    for line in format_rows(rows):
        logger.info(line)


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    rows = intent_report()
    path = intent_file()
    if rows is None:
        print(f"no intent report: {path} could not be read")
        return 0
    if not rows:
        print(f"{path}: declared intent matches effective daemon config")
        return 0
    print(f"{path}: {len(rows)} key(s) differ from effective daemon config")
    for line in format_rows(rows):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
