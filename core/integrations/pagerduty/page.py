"""Page PagerDuty when an AutoOps investigation proposes containment."""

import logging
from typing import Sequence, Tuple

import httpx

from core.config import get_integration_config, get_settings

logger = logging.getLogger(__name__)

EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
TIMEOUT_S = 5  # runs on the terminal request's thread


def page_proposed_containment(
    case_id: str, run_id: str, actions: Sequence[Tuple[str, str]]
) -> None:
    """One page for a case, naming each ``(title, target)`` proposed on it.

    Never raises: a PagerDuty failure must not fail the terminal or hold up the
    approvals. The run id is the dedup key, a second guard against double pages.
    """
    try:
        if not actions or not get_settings().daemon_pagerduty_enabled:
            return
        config = get_integration_config("pagerduty")
        routing_key = config.get("routing_key") or config.get("integration_key")
        if not routing_key:
            logger.warning("PagerDuty not configured, skipping page for %s", case_id)
            return
        lines = [f"{title}: {target}" for title, target in actions]
        response = httpx.post(
            EVENTS_URL,
            json={
                "routing_key": routing_key,
                "event_action": "trigger",
                "dedup_key": f"vigil-run-{run_id}",
                "payload": {
                    "summary": f"Containment proposed on case {case_id}: "
                    + "; ".join(lines),
                    "source": "vigil",
                    "severity": "critical",
                    "custom_details": {
                        "case_id": case_id,
                        "run_id": run_id,
                        "proposed_actions": lines,
                    },
                },
            },
            timeout=TIMEOUT_S,
            follow_redirects=True,
        )
        if response.json().get("status") == "success":
            logger.info("Paged PagerDuty for case %s (run %s)", case_id, run_id)
        else:
            logger.error(
                "PagerDuty page for case %s failed: %s", case_id, response.text
            )
    except Exception as e:  # noqa: BLE001
        logger.error("PagerDuty page for case %s failed: %s", case_id, e)
